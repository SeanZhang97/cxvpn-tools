//! Narrow IP Helper boundary. No DNS, processes, profile writes or default routes.
use crate::util::AppResult;
use serde::{Deserialize, Serialize};
use std::{net::{IpAddr, Ipv4Addr, Ipv6Addr}, ptr::null};
use windows_sys::Win32::{NetworkManagement::{IpHelper::*, Ndis::NET_LUID_LH}, Networking::WinSock::*};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Route {
    pub luid: u64,
    pub ip: IpAddr,
    pub next: IpAddr,
    pub metric: u32,
}
impl Route {
    pub fn key(&self) -> String { format!("{}:{}:{}", self.luid, self.ip, self.next) }
    fn native(&self) -> MIB_IPFORWARD_ROW2 {
        let mut row = MIB_IPFORWARD_ROW2::default();
        unsafe { InitializeIpForwardEntry(&mut row); }
        row.InterfaceLuid = NET_LUID_LH { Value: self.luid };
        row.DestinationPrefix.Prefix = address(self.ip);
        row.DestinationPrefix.PrefixLength = if self.ip.is_ipv4() { 32 } else { 128 };
        row.NextHop = address(self.next);
        row.Metric = self.metric;
        row.Protocol = 3; // MIB_IPPROTO_NETMGMT
        row.AutoconfigureAddress = false;
        row.Immortal = false;
        row.Publish = false;
        row.Loopback = false;
        row
    }
}

pub trait RouteOs: Send {
    fn vpn_connected(&self, name: &str) -> AppResult<bool>;
    fn interface(&self, name: &str, ip: IpAddr, vpn: bool) -> AppResult<(u64, u32)>;
    fn best(&self, luid: u64, ip: IpAddr) -> AppResult<Option<Route>>;
    fn matches(&self, route: &Route) -> AppResult<bool>;
    fn create(&mut self, route: &Route) -> AppResult<bool>;
    fn remove(&mut self, route: &Route) -> AppResult<()>;
}
pub struct WindowsRoutes;
impl RouteOs for WindowsRoutes {
    fn vpn_connected(&self, name: &str) -> AppResult<bool> {
        let mut table = std::ptr::null_mut();
        check(unsafe { GetIfTable2(&mut table) }, "read VPN connection state")?;
        if table.is_null() { return Err("VPN interface table unavailable".into()); }
        let result = unsafe {
            let rows = std::slice::from_raw_parts((*table).Table.as_ptr(), (*table).NumEntries as usize);
            let found = rows.iter().find(|r| {
                let len = r.Alias.iter().position(|c| *c == 0).unwrap_or(r.Alias.len());
                String::from_utf16_lossy(&r.Alias[..len]).eq_ignore_ascii_case(name)
            });
            let result = match found {
                Some(r) if !matches!(r.Type, 23 | 131) || name == "CXVPN-TUN" => Err("selected interface is not a VPN".into()),
                Some(r) => Ok(r.OperStatus == 1),
                None => Ok(false),
            };
            FreeMibTable(table.cast());
            result
        };
        result
    }
    fn interface(&self, name: &str, ip: IpAddr, vpn: bool) -> AppResult<(u64, u32)> {
        let wide: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
        let mut luid = NET_LUID_LH::default();
        check(unsafe { ConvertInterfaceAliasToLuid(wide.as_ptr(), &mut luid) }, "resolve interface")?;
        let mut link = MIB_IF_ROW2::default();
        link.InterfaceLuid = luid;
        check(unsafe { GetIfEntry2(&mut link) }, "read link")?;
        if link.OperStatus != 1 || (vpn && (!matches!(link.Type, 23 | 131) || name == "CXVPN-TUN")) {
            return Err("VPN interface unavailable or not a VPN".into());
        }
        let mut row = MIB_IPINTERFACE_ROW::default();
        row.Family = if ip.is_ipv4() { AF_INET } else { AF_INET6 };
        row.InterfaceLuid = luid;
        check(unsafe { GetIpInterfaceEntry(&mut row) }, "read IP interface")?;
        if !row.Connected { return Err("interface address family disconnected".into()); }
        if vpn && ip.is_ipv6() {
            let mut table = std::ptr::null_mut();
            check(unsafe { GetUnicastIpAddressTable(AF_INET6, &mut table) }, "read IPv6 source addresses")?;
            if table.is_null() { return Err("VPN has no IPv6 source address".into()); }
            let available = unsafe {
                let values = std::slice::from_raw_parts((*table).Table.as_ptr(), (*table).NumEntries as usize);
                let found = values.iter().any(|v| v.InterfaceLuid.Value == luid.Value && v.DadState == 4
                    && !v.SkipAsSource && v.ValidLifetime > 0 && from_address(v.Address).is_ok_and(valid_target));
                FreeMibTable(table.cast());
                found
            };
            if !available { return Err("VPN has no usable IPv6 source address".into()); }
        }
        Ok((unsafe { luid.Value }, row.Metric))
    }
    fn best(&self, luid: u64, ip: IpAddr) -> AppResult<Option<Route>> {
        let mut row = MIB_IPFORWARD_ROW2::default();
        let mut source = SOCKADDR_INET::default();
        let interface = NET_LUID_LH { Value: luid };
        let code = unsafe { GetBestRoute2(if luid == 0 { null() } else { &interface }, 0,
            null(), &address(ip), 0, &mut row, &mut source) };
        if matches!(code, 1168 | 1231) { return Ok(None); }
        check(code, "find route")?;
        Ok(Some(Route { luid: unsafe { row.InterfaceLuid.Value }, ip,
            next: from_address(row.NextHop)?, metric: row.Metric }))
    }
    fn matches(&self, route: &Route) -> AppResult<bool> {
        let mut row = route.native();
        let code = unsafe { GetIpForwardEntry2(&mut row) };
        if code == 1168 { return Ok(false); }
        if matches!(code, 2 | 87) {
            let mut link = MIB_IF_ROW2::default();
            link.InterfaceLuid = row.InterfaceLuid;
            if matches!(unsafe { GetIfEntry2(&mut link) }, 2 | 87 | 1168) { return Ok(false); }
        }
        check(code, "read host route")?;
        Ok(row.Metric == route.metric && row.Protocol == 3 && !row.Publish
            && !row.Immortal && !row.AutoconfigureAddress && !row.Loopback)
    }
    fn create(&mut self, route: &Route) -> AppResult<bool> {
        let code = unsafe { CreateIpForwardEntry2(&route.native()) };
        if code == 5010 { return Ok(false); } // existing row remains user-owned
        check(code, "create host route")?;
        Ok(true)
    }
    fn remove(&mut self, route: &Route) -> AppResult<()> {
        // External edits relinquish ownership; never rewrite or delete the edited row.
        if !self.matches(route)? { return Ok(()); }
        let code = unsafe { DeleteIpForwardEntry2(&route.native()) };
        if code == 1168 { return Ok(()); }
        check(code, "delete host route")
    }
}
fn check(code: u32, operation: &str) -> AppResult<()> {
    if code == 0 { Ok(()) } else { Err(format!("{operation}: Win32 {code}")) }
}
fn address(ip: IpAddr) -> SOCKADDR_INET {
    match ip {
        IpAddr::V4(ip) => {
            let mut v = SOCKADDR_IN::default();
            v.sin_family = AF_INET;
            v.sin_addr.S_un.S_addr = u32::from_ne_bytes(ip.octets());
            SOCKADDR_INET { Ipv4: v }
        },
        IpAddr::V6(ip) => {
            let mut v = SOCKADDR_IN6::default();
            v.sin6_family = AF_INET6;
            v.sin6_addr.u.Byte = ip.octets();
            SOCKADDR_INET { Ipv6: v }
        },
    }
}
fn from_address(value: SOCKADDR_INET) -> AppResult<IpAddr> {
    unsafe {
        match value.si_family {
            AF_INET => Ok(Ipv4Addr::from(value.Ipv4.sin_addr.S_un.S_addr.to_ne_bytes()).into()),
            AF_INET6 => Ok(Ipv6Addr::from(value.Ipv6.sin6_addr.u.Byte).into()),
            _ => Err("invalid route address family".into()),
        }
    }
}

pub fn valid_target(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v) => {
            let o = v.octets();
            !v.is_unspecified() && !v.is_loopback() && !v.is_multicast() && !v.is_link_local()
                && !v.is_broadcast() && o[0] != 0 && o[0] < 224
                && !(o[0] == 198 && matches!(o[1], 18 | 19))
        },
        IpAddr::V6(v) => !v.is_unspecified() && !v.is_loopback() && !v.is_multicast()
            && !v.is_unicast_link_local() && v.to_ipv4_mapped().is_none()
            && (v.segments()[..4] != [0xfdfe, 0xdcba, 0x9876, 0]),
    }
}
