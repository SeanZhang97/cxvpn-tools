"""Apply the bounded Windows VPN hook to the pinned upstream source only."""
from pathlib import Path
import shutil


def apply(root):
    root = Path(root)

    def replace(path, before, after):
        target = root / path
        text = target.read_text(encoding='utf-8')
        if text.count(before) != 1:
            raise ValueError(f'Upstream context mismatch: {path}')
        target.write_text(text.replace(before, after), encoding='utf-8')

    replace('adapter/outbound/direct.go', 'loopBack *loopback.Detector',
            'loopBack *loopback.Detector\n managedRoute bool')
    replace('adapter/outbound/direct.go', 'Name string `proxy:"name"`',
            'Name string `proxy:"name"`\n ManagedRoute bool `proxy:"cxvpn-managed-route,omitempty"`')
    replace('adapter/outbound/direct.go', 'opts := d.DialOptions()',
            'opts := append(d.DialOptions(), dialer.WithManagedRoute(d.managedRoute))')
    replace('adapter/outbound/direct.go', 'dialer.NewDialer(d.DialOptions()...)',
            'dialer.NewDialer(append(d.DialOptions(), dialer.WithManagedRoute(d.managedRoute))...)')
    replace('adapter/outbound/direct.go', 'func NewDirectWithOption(option DirectOption) *Direct {\n\treturn &Direct{',
            'func NewDirectWithOption(option DirectOption) *Direct {\n\treturn &Direct{\n managedRoute: option.ManagedRoute,')
    replace('component/dialer/options.go', 'interfaceName string', 'interfaceName string\n managedRoute bool')
    replace('component/dialer/options.go', 'func WithInterface(name string) Option {',
            'func WithManagedRoute(enabled bool) Option { return func(opt *option) { opt.managedRoute = enabled } }\n\nfunc WithInterface(name string) Option {')
    replace('component/dialer/dialer.go', '"github.com/metacubex/mihomo/component/resolver"',
            '"github.com/metacubex/mihomo/component/resolver"\n "github.com/metacubex/mihomo/component/vpnroute"')
    replace('component/dialer/dialer.go',
            'func dialContext(ctx context.Context, network string, destination netip.Addr, port string, opt option) (net.Conn, error) {',
            'func dialContext(ctx context.Context, network string, destination netip.Addr, port string, opt option) (result net.Conn, resultErr error) {')
    replace('component/dialer/dialer.go', 'address = net.JoinHostPort(destination.String(), port)',
            '''address = net.JoinHostPort(destination.String(), port)
 if opt.managedRoute {
  if opt.interfaceName == "" || opt.fallbackBind || DefaultSocketHook != nil || opt.netDialer != nil {
   return nil, errors.New("managed VPN requires explicit interface-bound dialer")
  }
  lease, err := vpnroute.Acquire(ctx, opt.interfaceName, destination)
  if err != nil { return nil, err }
  opt.interfaceName = lease.Interface
  defer func(){ if resultErr != nil || result == nil { lease.Release() } else { result = vpnroute.WrapConn(result, lease) } }()
  opt.mpTcp = false // additional MPTCP subflows do not have independent route leases
 }''')
    replace('component/dialer/dialer.go',
            'func ListenPacket(ctx context.Context, network, address string, rAddrPort netip.AddrPort, options ...Option) (net.PacketConn, error) {\n\topt := applyOptions(options...)',
            '''func ListenPacket(ctx context.Context, network, address string, rAddrPort netip.AddrPort, options ...Option) (result net.PacketConn, resultErr error) {
 opt := applyOptions(options...)
 if opt.managedRoute {
  if opt.interfaceName == "" || opt.fallbackBind || DefaultSocketHook != nil {
   return nil, errors.New("managed VPN requires explicit interface-bound UDP")
  }
  vpn := opt.interfaceName
  lease, err := vpnroute.Acquire(ctx, vpn, rAddrPort.Addr())
  if err != nil { return nil, err }
  opt.interfaceName = lease.Interface
  defer func(){ if resultErr != nil || result == nil { lease.Release() } else { result = vpnroute.WrapPacket(result, vpn, rAddrPort.Addr(), lease) } }()
 }''')
    replace('hub/route/server.go', 'render.M{"meta": C.Meta, "version": C.Version}',
            'render.M{"meta": C.Meta, "version": C.Version, "cxvpn-vpn-routes": 2}')
    overlay = Path(__file__).parent / 'overlay'
    for source in overlay.rglob('*.go'):
        target = root / source.relative_to(overlay)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


if __name__ == '__main__':
    import sys
    apply(sys.argv[1])
