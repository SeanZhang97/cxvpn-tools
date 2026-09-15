//! Route leases belong to a running core generation, never to a DNS TTL.
use crate::{util::{atomic_write, AppResult}, vpn_route_os::{valid_target, Route, RouteOs, WindowsRoutes}};
use serde::{Deserialize, Serialize};
use std::{collections::{HashMap, HashSet}, fs, io::Write, net::IpAddr, path::PathBuf,
    sync::{Arc, Mutex}, time::{Duration, Instant}};

pub type Shared = Arc<Mutex<RouteState>>;
pub const MAX_ROUTES: usize = 4096;
const MAX_LEASES: usize = 16384;
const IDLE: Duration = Duration::from_secs(60);

#[derive(Clone, Default)]
pub struct Policy {
    pub pid: u32,
    pub epoch: String,
    pub vpns: HashSet<String>,
    pub guard: String,
    pub physical: String,
    pub fake_ranges: Vec<(IpAddr, u8)>,
}
impl Policy {
    pub fn from_config(data: &[u8], pid: u32, epoch: String) -> AppResult<Self> {
        let cfg: serde_json::Value = serde_json::from_slice(data).map_err(|_| "invalid route policy")?;
        let mut vpns = HashSet::new();
        let mut physical = String::new();
        for p in cfg["proxies"].as_array().ok_or("missing proxies")? {
            if p["name"] == "PHYSICAL" { physical = p["interface-name"].as_str().unwrap_or("").into(); }
            if p["cxvpn-managed-route"] == true {
                let name = p["interface-name"].as_str().ok_or("missing VPN interface")?;
                if p["type"] != "direct" || name.is_empty() || name.len() > 512
                    || name.contains('\0') || name == "CXVPN-TUN" || p["name"] == "PHYSICAL" {
                    return Err("invalid managed VPN proxy".into());
                }
                vpns.insert(name.to_owned());
            }
        }
        let guard = if cfg["tun"]["enable"] == true { "CXVPN-TUN".into() } else { physical.clone() };
        if !vpns.is_empty() && (guard.is_empty() || physical.is_empty() || physical == "CXVPN-TUN"
            || vpns.contains(&guard) || vpns.contains(&physical)) {
            return Err("managed VPN requires separate capture/physical interface".into());
        }
        let mut fake_ranges = Vec::new();
        for key in ["fake-ip-range", "fake-ip-range6"] {
            if let Some(cidr) = cfg["dns"][key].as_str() {
                let (ip, bits) = cidr.split_once('/').ok_or("invalid fake IP range")?;
                let ip: IpAddr = ip.parse().map_err(|_| "invalid fake IP address")?;
                let bits: u8 = bits.parse().map_err(|_| "invalid fake IP prefix")?;
                if bits > if ip.is_ipv4() { 32 } else { 128 } { return Err("invalid fake IP prefix length".into()); }
                fake_ranges.push((ip, bits));
            }
        }
        Ok(Self { pid, epoch, vpns, guard, physical, fake_ranges })
    }
    pub fn permits(&self, pid: u32, epoch: &str) -> bool {
        pid != 0 && pid == self.pid && !epoch.is_empty() && epoch == self.epoch
    }
    fn is_fake(&self, ip: IpAddr) -> bool {
        self.fake_ranges.iter().any(|(base, bits)| match (ip, base) {
            (IpAddr::V4(ip), IpAddr::V4(base)) => *bits == 0 || (u32::from(ip) >> (32-bits)) == (u32::from(*base) >> (32-bits)),
            (IpAddr::V6(ip), IpAddr::V6(base)) => *bits == 0 || (u128::from(ip) >> (128-bits)) == (u128::from(*base) >> (128-bits)),
            _ => false,
        })
    }
}

#[derive(Clone, Serialize, Deserialize)]
struct Owned { route: Route, guard: bool }
struct Entry { owned: Owned, refs: usize, idle: Instant }
struct Lease { vpn: String, ip: IpAddr, keys: Vec<String>, interface: String }

pub struct RouteState {
    pub policy: Policy,
    os: Box<dyn RouteOs>,
    journal: PathBuf,
    entries: HashMap<String, Entry>,
    leases: HashMap<String, Lease>,
    cancelled: HashMap<String, Instant>,
}
impl RouteState {
    pub fn new(base: &std::path::Path) -> AppResult<Shared> {
        Ok(Arc::new(Mutex::new(Self::open(base.join("vpn-routes.json"), Box::new(WindowsRoutes))?)))
    }
    fn open(journal: PathBuf, os: Box<dyn RouteOs>) -> AppResult<Self> {
        let saved: Vec<Owned> = match fs::read(&journal) {
            Ok(bytes) => serde_json::from_slice(&bytes).map_err(|_| "route recovery journal is corrupt; preserved")?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Vec::new(),
            Err(e) => return Err(format!("read route journal: {e}")),
        };
        if saved.len() > MAX_ROUTES { return Err("route recovery journal exceeds capacity".into()); }
        let mut result = Self { policy: Policy::default(), os, journal, entries: HashMap::new(),
            leases: HashMap::new(), cancelled: HashMap::new() };
        for item in saved {
            if !valid_target(item.route.ip) || item.route.luid == 0 || item.route.ip.is_ipv4() != item.route.next.is_ipv4() {
                return Err("invalid route recovery record; preserved".into());
            }
            result.entries.insert(item.route.key(), Entry { owned: item, refs: 0, idle: Instant::now() });
        }
        result.sweep(true)?;
        Ok(result)
    }
    fn save(&self) -> AppResult<()> {
        let saved: Vec<_> = self.entries.values().map(|v| &v.owned).collect();
        atomic_write(&self.journal, &serde_json::to_vec(&saved).map_err(|_| "encode route journal")?)
    }
    pub fn log(&self, message: &str) {
        if let Some(base) = self.journal.parent() {
            let log = base.join("vpn-routes.log");
            if fs::metadata(&log).is_ok_and(|m| m.len() > 2 * 1024 * 1024) {
                let _ = fs::remove_file(base.join("vpn-routes.previous.log"));
                let _ = fs::rename(&log, base.join("vpn-routes.previous.log"));
            }
            if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(base.join("vpn-routes.log")) {
                let _ = writeln!(f, "{} {message}", std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs());
            }
        }
    }
    pub fn reset(&mut self, policy: Policy) -> AppResult<()> {
        self.policy = Policy::default(); // reject late requests before recovery
        self.leases.clear();
        self.cancelled.clear();
        for row in self.entries.values_mut() { row.refs = 0; }
        self.sweep(true)?;
        self.policy = policy;
        Ok(())
    }
    fn ensure_owned(&mut self, route: Route, guard: bool) -> AppResult<Option<String>> {
        let key = route.key();
        if let Some(old) = self.entries.get(&key) {
            if old.owned.route == route && self.os.matches(&route)? { return Ok(Some(key)); }
            // The same identity changed externally. It must no longer be deleted by us.
            self.entries.remove(&key);
            self.save()?;
        }
        if self.entries.len() >= MAX_ROUTES { return Err("managed route capacity reached".into()); }
        if self.os.matches(&route)? { return Ok(None); }
        let refs = self.leases.values().filter(|l| l.keys.contains(&key)).count();
        self.entries.insert(key.clone(), Entry { owned: Owned { route: route.clone(), guard },
            refs, idle: Instant::now() });
        // Persist intent before touching the OS; recovery is idempotent and identity checked.
        if let Err(error) = self.save() { self.entries.remove(&key); return Err(error); }
        match self.os.create(&route) {
            Ok(true) => {
                if !self.os.matches(&route)? { return Err("created route readback mismatch".into()); }
                self.log(&format!("route created target={} interface={} guard={guard}", route.ip, route.luid));
                Ok(Some(key))
            },
            Ok(false) => { self.entries.remove(&key); self.save()?; Ok(None) },
            Err(error) => {
                self.entries.remove(&key); self.save()?;
                Err(error)
            },
        }
    }
    pub fn acquire(&mut self, id: &str, vpn: &str, ip: IpAddr, deadline: Instant) -> AppResult<()> {
        self.log(&format!("request claimed/start id={id} target={ip} timeout=2s"));
        if id.is_empty() || id.len() > 20 || !id.bytes().all(|b| b.is_ascii_digit())
            || !valid_target(ip) || self.policy.is_fake(ip) || !self.policy.vpns.contains(vpn) {
            return Err("invalid target, request id or VPN policy".into());
        }
        if self.cancelled.contains_key(id) { return Err("route request cancelled".into()); }
        if let Some(lease) = self.leases.get(id) {
            return if lease.vpn == vpn && lease.ip == ip { Ok(()) } else { Err("lease identity mismatch".into()) };
        }
        if self.leases.len() >= MAX_LEASES { return Err("route lease capacity reached".into()); }
        if Instant::now() >= deadline { return Err("route prepare deadline exceeded".into()); }
        if !self.os.vpn_connected(vpn)? {
            let physical = self.policy.physical.clone();
            let (luid, _) = self.os.interface(&physical, ip, false)?;
            if physical.is_empty() || self.os.best(luid, ip)?.is_none() {
                return Err("physical fallback route unavailable".into());
            }
            if Instant::now() >= deadline { return Err("route prepare deadline exceeded".into()); }
            self.leases.insert(id.into(), Lease { vpn: vpn.into(), ip, keys: Vec::new(), interface: physical });
            self.log(&format!("request success id={id} target={ip} outbound=physical reason=VPN-disconnected"));
            return Ok(());
        }
        let (luid, _) = self.os.interface(vpn, ip, true)?;
        let mut keys = Vec::new();
        let result = (|| -> AppResult<()> {
            let managed = self.entries.values().find(|e| !e.owned.guard && e.owned.route.luid == luid
                && e.owned.route.ip == ip).map(|e| e.owned.route.clone());
            if let Some(existing) = self.os.best(luid, ip)? {
                // A covering user route needs neither ownership nor a new global host route.
                if managed.as_ref().is_none_or(|r| r.next != existing.next || r.metric != existing.metric) {
                    return Ok(());
                }
            }
            let (guard_luid, guard_metric) = self.os.interface(&self.policy.guard, ip, false)?;
            if guard_metric >= 50000 || guard_luid == luid { return Err("unsafe route guard interface".into()); }
            let baseline = self.os.best(guard_luid, ip)?.ok_or("no capture/physical route for target family")?;
            let guard = Route { luid: guard_luid, ip, next: baseline.next, metric: 7 };
            if let Some(key) = self.ensure_owned(guard.clone(), true)? { keys.push(key); }
            let effective = self.os.best(guard_luid, ip)?.ok_or("route guard unavailable")?;
            if effective.next != guard.next || effective.metric + guard_metric >= 60000 {
                return Err("route guard priority is not safe".into());
            }
            if Instant::now() >= deadline { return Err("route prepare deadline exceeded".into()); }
            let next = if ip.is_ipv4() { "0.0.0.0" } else { "::" }.parse().unwrap();
            let target = Route { luid, ip, next, metric: 60000 };
            if let Some(key) = self.ensure_owned(target.clone(), false)? { keys.push(key); }
            let effective = self.os.best(luid, ip)?.ok_or("VPN route readback unavailable")?;
            if effective.next != target.next { return Err("VPN route readback mismatch".into()); }
            // The unbound path must remain on the guard after the VPN host route was added.
            if self.os.best(0, ip)?.is_none_or(|r| r.luid != guard_luid) {
                return Err("unbound traffic would bypass its capture/physical path".into());
            }
            if Instant::now() >= deadline { return Err("route prepare deadline exceeded".into()); }
            Ok(())
        })();
        if let Err(error) = result {
            self.log(&format!("request failed id={id} target={ip}: {error}"));
            // Only unused records can be rolled back; other live leases remain protected.
            self.sweep(true)?;
            return Err(error);
        }
        for key in &keys { if let Some(e) = self.entries.get_mut(key) { e.refs += 1; } }
        self.leases.insert(id.into(), Lease { vpn: vpn.into(), ip, keys, interface: vpn.into() });
        self.log(&format!("request success id={id} target={ip}"));
        Ok(())
    }
    pub fn outbound(&self, id: &str) -> AppResult<String> {
        self.leases.get(id).map(|l| l.interface.clone()).ok_or_else(|| "route lease missing".into())
    }
    pub fn release(&mut self, id: &str) -> AppResult<()> {
        if let Some(lease) = self.leases.remove(id) {
            for key in lease.keys {
                if let Some(e) = self.entries.get_mut(&key) { e.refs = e.refs.saturating_sub(1); e.idle = Instant::now(); }
            }
            self.log(&format!("lease released id={id} target={}", lease.ip));
        }
        if self.cancelled.len() >= MAX_LEASES * 2 { return Err("cancellation capacity reached".into()); }
        self.cancelled.insert(id.into(), Instant::now());
        Ok(())
    }
    pub fn sweep(&mut self, force: bool) -> AppResult<()> {
        self.cancelled.retain(|_, stamp| stamp.elapsed() < Duration::from_secs(30));
        let mut due: Vec<_> = self.entries.iter().filter(|(_, e)| e.refs == 0 && (force || e.idle.elapsed() >= IDLE))
            .map(|(key, e)| (e.owned.guard, key.clone())).collect();
        due.sort(); // remove VPN routes before their protective routes
        let mut error = None;
        let mut changed = false;
        for (_, key) in due {
            let Some(entry) = self.entries.get(&key) else { continue; };
            if entry.owned.guard && self.entries.values().any(|e| !e.owned.guard && e.owned.route.ip == entry.owned.route.ip) { continue; }
            match self.os.remove(&entry.owned.route) {
                Ok(()) => { self.log(&format!("route cleaned target={} interface={}", entry.owned.route.ip, entry.owned.route.luid));
                    self.entries.remove(&key); changed = true; },
                Err(e) => { error = Some(e); },
            }
        }
        if changed { self.save()?; }
        if let Some(e) = error { return Err(e); }
        Ok(())
    }
}

#[cfg(test)]
#[path = "vpn_routes_tests.rs"]
mod tests;
