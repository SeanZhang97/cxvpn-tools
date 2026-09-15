use super::*;
use std::sync::atomic::{AtomicUsize, Ordering};
#[derive(Default)]
struct Mock {
    rows: HashMap<String, Route>,
    user: bool,
    fail_create: bool,
    fail_delete: bool,
    down: bool,
    state_error: bool,
    physical_down: bool,
    next_luid: u64,
}
struct Fake(Arc<Mutex<Mock>>);
impl RouteOs for Fake {
    fn vpn_connected(&self, name: &str) -> AppResult<bool> {
        let s = self.0.lock().unwrap();
        if s.state_error { return Err("state query failed".into()); }
        Ok(!s.down || name == "other")
    }
    fn interface(&self, name: &str, _: IpAddr, vpn: bool) -> AppResult<(u64, u32)> {
        let s = self.0.lock().unwrap();
        if vpn && s.down && name != "other" { return Err("VPN disconnected".into()); }
        if !vpn && s.physical_down { return Err("physical disconnected".into()); }
        Ok((if vpn { if name == "other" { 3 } else { s.next_luid.max(2) } } else { 1 }, 1))
    }
    fn best(&self, luid: u64, ip: IpAddr) -> AppResult<Option<Route>> {
        let s = self.0.lock().unwrap();
        if let Some(r) = s.rows.values().filter(|r| r.ip == ip && (luid == 0 || r.luid == luid)).min_by_key(|r| r.metric) {
            return Ok(Some(r.clone()));
        }
        if luid <= 1 || s.user {
            Ok(Some(Route { luid: luid.max(1), ip, next: "192.168.1.1".parse().unwrap(), metric: 256 }))
        } else { Ok(None) }
    }
    fn matches(&self, route: &Route) -> AppResult<bool> { Ok(self.0.lock().unwrap().rows.get(&route.key()) == Some(route)) }
    fn create(&mut self, route: &Route) -> AppResult<bool> {
        let mut s = self.0.lock().unwrap();
        if s.fail_create && route.luid != 1 { return Err("injected create failure".into()); }
        if s.rows.contains_key(&route.key()) { return Ok(false); }
        s.rows.insert(route.key(), route.clone()); Ok(true)
    }
    fn remove(&mut self, route: &Route) -> AppResult<()> {
        let mut s = self.0.lock().unwrap();
        if s.fail_delete && route.luid != 1 { return Err("injected delete failure".into()); }
        if s.rows.get(&route.key()) == Some(route) { s.rows.remove(&route.key()); }
        Ok(())
    }
}
static NEXT: AtomicUsize = AtomicUsize::new(0);
struct Fixture { state: RouteState, os: Arc<Mutex<Mock>>, root: PathBuf }
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("cxvpn-route-test-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
        fs::create_dir_all(&root).unwrap();
        let os = Arc::new(Mutex::new(Mock::default()));
        let mut state = RouteState::open(root.join("journal.json"), Box::new(Fake(os.clone()))).unwrap();
        state.policy = Policy { pid: 123, epoch: "epoch".into(), vpns: ["vpn".into(), "other".into()].into(), guard: "physical".into(), physical: "physical".into(), ..Policy::default() };
        Self { state, os, root }
    }
    fn acquire(&mut self, id: &str, vpn: &str, ip: &str) -> AppResult<()> {
        self.state.acquire(id, vpn, ip.parse().unwrap(), Instant::now()+Duration::from_secs(2))
    }
}
impl Drop for Fixture { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.root); } }

#[test]
fn first_target_gets_guard_and_vpn_before_success() {
    let mut f=Fixture::new(); f.acquire("1","vpn","203.0.113.8").unwrap();
    assert_eq!(f.os.lock().unwrap().rows.len(),2);
    assert_eq!(f.state.leases.len(),1);
    assert!(f.state.entries.values().all(|e|e.refs==1));
}
#[test]
fn shared_ip_stays_until_all_connections_release() {
    let mut f=Fixture::new();
    f.acquire("1","vpn","203.0.113.8").unwrap();f.acquire("2","vpn","203.0.113.8").unwrap();
    f.state.release("1").unwrap();f.state.sweep(true).unwrap();assert_eq!(f.state.entries.len(),2);
    f.state.release("2").unwrap();f.state.sweep(false).unwrap();assert_eq!(f.state.entries.len(),2);
    f.state.sweep(true).unwrap();assert!(f.os.lock().unwrap().rows.is_empty());
}
#[test]
fn dns_change_keeps_old_active_connection_route() {
    let mut f=Fixture::new();f.acquire("1","vpn","203.0.113.8").unwrap();f.acquire("2","vpn","203.0.113.9").unwrap();
    f.state.release("2").unwrap();f.state.sweep(true).unwrap();
    assert_eq!(f.state.entries.len(),2);assert!(f.state.entries.values().all(|e|e.owned.route.ip.to_string()=="203.0.113.8"));
}
#[test]
fn two_vpns_share_guard_but_not_target_interface() {
    let mut f=Fixture::new();f.acquire("1","vpn","203.0.113.8").unwrap();f.acquire("2","other","203.0.113.8").unwrap();
    assert_eq!(f.state.entries.len(),3);f.state.release("1").unwrap();f.state.sweep(true).unwrap();
    assert_eq!(f.state.entries.len(),2);assert!(f.state.entries.values().any(|e|e.owned.route.luid==3));
}
#[test]
fn user_covering_route_is_never_owned() {
    let mut f=Fixture::new();f.os.lock().unwrap().user=true;f.acquire("1","vpn","203.0.113.8").unwrap();
    assert!(f.state.entries.is_empty());f.state.reset(Policy::default()).unwrap();assert!(f.os.lock().unwrap().user);
}
#[test]
fn partial_failure_removes_guard_but_protects_other_lease() {
    let mut f=Fixture::new();f.acquire("1","vpn","203.0.113.8").unwrap();f.os.lock().unwrap().fail_create=true;
    assert!(f.acquire("2","vpn","203.0.113.9").is_err());assert_eq!(f.state.entries.len(),2);
}
#[test]
fn cancellation_before_acquire_prevents_late_route_creation() {
    let mut f=Fixture::new();f.state.release("1").unwrap();assert!(f.acquire("1","vpn","203.0.113.8").is_err());
    assert!(f.os.lock().unwrap().rows.is_empty());
}
#[test]
fn expired_request_does_not_touch_routes() {
    let mut f=Fixture::new();assert!(f.state.acquire("1","vpn","203.0.113.8".parse().unwrap(),Instant::now()-Duration::from_secs(1)).is_err());
    assert!(f.state.entries.is_empty());
}
#[test]
fn duplicate_request_is_idempotent_and_identity_checked() {
    let mut f=Fixture::new();f.acquire("1","vpn","203.0.113.8").unwrap();f.acquire("1","vpn","203.0.113.8").unwrap();
    assert!(f.state.entries.values().all(|e|e.refs==1));assert!(f.acquire("1","vpn","203.0.113.9").is_err());
}
#[test]
fn delete_failure_keeps_guard_and_recovery_records() {
    let mut f=Fixture::new();f.acquire("1","vpn","203.0.113.8").unwrap();f.state.release("1").unwrap();
    f.os.lock().unwrap().fail_delete=true;assert!(f.state.sweep(true).is_err());assert_eq!(f.state.entries.len(),2);
    f.os.lock().unwrap().fail_delete=false;f.state.sweep(true).unwrap();assert!(f.state.entries.is_empty());
}
#[test]
fn restart_cleans_only_unchanged_owned_routes() {
    let mut f=Fixture::new();f.acquire("1","vpn","203.0.113.8").unwrap();
    {let mut s=f.os.lock().unwrap();s.rows.values_mut().find(|r|r.luid==2).unwrap().metric=42;}
    let recovered=RouteState::open(f.state.journal.clone(),Box::new(Fake(f.os.clone()))).unwrap();
    assert!(recovered.entries.is_empty());assert_eq!(f.os.lock().unwrap().rows.len(),1);
}
#[test]
fn externally_deleted_route_is_recreated_without_losing_live_refs() {
    let mut f=Fixture::new();f.acquire("1","vpn","203.0.113.8").unwrap();f.os.lock().unwrap().rows.clear();
    f.acquire("2","vpn","203.0.113.8").unwrap();f.state.release("2").unwrap();f.state.sweep(true).unwrap();
    assert_eq!(f.state.entries.len(),2);assert!(f.state.entries.values().all(|e|e.refs==1));
}
#[test]
fn down_vpn_uses_physical_and_reconnected_vpn_is_selected_for_new_connections() {
    let mut f=Fixture::new();f.os.lock().unwrap().down=true;
    f.acquire("1","vpn","203.0.113.8").unwrap();
    assert_eq!(f.state.outbound("1").unwrap(),"physical");assert!(f.state.entries.is_empty());
    f.os.lock().unwrap().down=false;
    f.acquire("2","vpn","203.0.113.8").unwrap();
    assert_eq!(f.state.outbound("2").unwrap(),"vpn");assert_eq!(f.state.outbound("1").unwrap(),"physical");
    assert_eq!(f.state.entries.len(),2);
}

#[test]
fn state_or_connected_route_error_does_not_fallback() {
    let mut f=Fixture::new();f.os.lock().unwrap().state_error=true;
    assert!(f.acquire("1","vpn","203.0.113.8").is_err());assert!(f.state.leases.is_empty());
    {let mut s=f.os.lock().unwrap();s.state_error=false;s.fail_create=true;}
    assert!(f.acquire("2","vpn","203.0.113.8").is_err());assert!(f.state.leases.is_empty());
}
#[test]
fn down_vpn_requires_available_physical_interface() {
    let mut f=Fixture::new();{let mut s=f.os.lock().unwrap();s.down=true;s.physical_down=true;}
    assert!(f.acquire("1","vpn","203.0.113.8").is_err());assert!(f.state.leases.is_empty());
}
#[test]
fn vpn_connection_decisions_are_independent() {
    let mut f=Fixture::new();f.os.lock().unwrap().down=true;
    f.acquire("1","vpn","203.0.113.8").unwrap();
    // The other VPN remains connected, while the first one uses physical.
    assert!(f.state.os.vpn_connected("other").unwrap());
    f.acquire("2","other","203.0.113.8").unwrap();
    assert_eq!(f.state.outbound("1").unwrap(),"physical");assert_eq!(f.state.outbound("2").unwrap(),"other");
}
#[test]
fn reconnected_vpn_uses_fresh_interface_identity() {
    let mut f=Fixture::new();f.acquire("1","vpn","203.0.113.8").unwrap();f.os.lock().unwrap().next_luid=9;
    f.acquire("2","vpn","203.0.113.8").unwrap();assert!(f.state.entries.values().any(|e|e.owned.route.luid==9));
}
#[test]
fn rejects_fake_loopback_multicast_and_mapped_addresses() {
    for ip in ["0.0.0.0","127.0.0.1","198.18.0.5","198.19.1.1","169.254.1.2","224.0.0.1","::1","ff02::1","fe80::1","::ffff:127.0.0.1","fdfe:dcba:9876::2"] {
        assert!(!valid_target(ip.parse().unwrap()),"{ip}");
    }
    for ip in ["10.1.2.3","140.210.72.160","2606:4700::1111"] {assert!(valid_target(ip.parse().unwrap()));}
}
#[test]
fn process_generation_and_unicode_policy_are_preserved() {
    let name="公司 e\u{301} \u{1f1e8}\u{1f1f3}";
    let cfg=serde_json::json!({"proxies":[{"name":"PHYSICAL","interface-name":"网卡"},{"name":"VPN-1","type":"direct","interface-name":name,"cxvpn-managed-route":true}],"tun":{"enable":true}});
    let p=Policy::from_config(&serde_json::to_vec(&cfg).unwrap(),123,"epoch".into()).unwrap();
    assert!(p.vpns.contains(name));assert!(p.permits(123,"epoch"));assert!(!p.permits(124,"epoch"));assert!(!p.permits(123,"old"));
}
#[test]
fn corrupt_journal_is_preserved_and_fails_closed() {
    let f=Fixture::new();fs::write(&f.state.journal,b"invalid").unwrap();
    assert!(RouteState::open(f.state.journal.clone(),Box::new(Fake(f.os.clone()))).is_err());assert_eq!(fs::read(&f.state.journal).unwrap(),b"invalid");
}
#[test]
fn journal_failure_prevents_os_mutation() {
    let mut f=Fixture::new();fs::create_dir(&f.state.journal).unwrap();assert!(f.acquire("1","vpn","203.0.113.8").is_err());
    assert!(f.os.lock().unwrap().rows.is_empty());
}

#[test]
fn capacity_rejects_new_lease_without_mutation() {
    let mut f=Fixture::new();
    for n in 0..MAX_LEASES {f.state.leases.insert(n.to_string(),Lease{vpn:"vpn".into(),ip:"203.0.113.8".parse().unwrap(),keys:Vec::new(),interface:"vpn".into()});}
    assert!(f.acquire("99999","vpn","203.0.113.9").is_err());assert!(f.os.lock().unwrap().rows.is_empty());
}

#[test]
#[ignore = "requires explicit elevated Windows route validation"]
fn native_route_lifecycle() {
    assert_eq!(std::env::var("CXVPN_ROUTE_INTEGRATION").as_deref(),Ok("1"));
    let vpn=std::env::var("CXVPN_ROUTE_TEST_VPN").unwrap();
    let root=std::env::temp_dir().join(format!("cxvpn-route-native-{}",std::process::id()));
    let mut state=RouteState::open(root.join("journal.json"),Box::new(WindowsRoutes)).unwrap();
    state.policy=Policy{pid:std::process::id(),epoch:"native".into(),vpns:[vpn.clone()].into(),guard:"CXVPN-TUN".into(),..Policy::default()};
    let result=state.acquire("1",&vpn,"203.0.113.123".parse().unwrap(),Instant::now()+Duration::from_secs(2));
    let count=state.entries.len();
    let release=state.release("1");
    let cleanup=state.sweep(true);
    assert!(cleanup.is_ok(),"cleanup failed: {cleanup:?}");assert!(release.is_ok());
    assert!(result.is_ok(),"native preparation failed: {result:?}");assert_eq!(count,2);
    assert!(state.entries.is_empty());let _=fs::remove_dir_all(root);
}

#[test]
fn configured_fake_ranges_are_never_installed_as_real_targets() {
    let mut f=Fixture::new();f.state.policy.fake_ranges=vec![("192.0.2.1".parse().unwrap(),24),("fd00:1::1".parse().unwrap(),64)];
    assert!(f.acquire("1","vpn","192.0.2.7").is_err());assert!(f.acquire("2","vpn","fd00:1::7").is_err());
    assert!(f.state.entries.is_empty());
}

#[test]
#[ignore = "requires an explicitly selected real physical interface"]
fn native_missing_vpn_selects_physical_without_route_mutation() {
    let physical=std::env::var("CXVPN_ROUTE_TEST_PHYSICAL").unwrap();
    let missing=format!("CXVPN-offline-test-{}",std::process::id());
    let root=std::env::temp_dir().join(format!("cxvpn-fallback-native-{}",std::process::id()));
    let mut state=RouteState::open(root.join("journal.json"),Box::new(WindowsRoutes)).unwrap();
    state.policy=Policy{vpns:[missing.clone()].into(),physical:physical.clone(),guard:"CXVPN-TUN".into(),..Policy::default()};
    state.acquire("1",&missing,"203.0.113.123".parse().unwrap(),Instant::now()+Duration::from_secs(2)).unwrap();
    assert_eq!(state.outbound("1").unwrap(),physical);
    assert!(state.entries.is_empty());assert!(!state.journal.exists());
    state.release("1").unwrap();state.sweep(true).unwrap();assert!(state.leases.is_empty());
    if root.exists(){let _=fs::remove_dir_all(root);}
}
