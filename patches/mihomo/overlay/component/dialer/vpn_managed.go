package dialer

import (
	"context"
	"errors"
	"net"
	"net/netip"
	"strings"

	"github.com/metacubex/mihomo/component/vpnroute"
)

func managedDialContext(ctx context.Context, network string, ip netip.Addr, port string, opt option) (net.Conn, error) {
	if opt.interfaceName == "" || opt.fallbackBind || DefaultSocketHook != nil || opt.netDialer != nil {
		return nil, errors.New("managed VPN requires explicit interface-bound dialer")
	}
	vpn := opt.interfaceName
	opt.managedRoute = false
	opt.mpTcp = false
	allowed := opt.vpnFallback && strings.HasPrefix(network, "tcp") && vpnroute.PublicTarget(ip)
	if allowed {
		opt.tfo = false
	} // TFO may send application bytes before Dial returns.
	dial := func(ctx context.Context, physical bool) (net.Conn, error) {
		acquire := vpnroute.Acquire
		if physical {
			acquire = vpnroute.AcquirePhysical
		}
		lease, err := acquire(ctx, vpn, ip)
		if err != nil {
			return nil, err
		}
		selected := opt
		selected.interfaceName = lease.Interface
		selected.managedBind = true
		c, err := dialContext(ctx, network, ip, port, selected)
		if err != nil {
			if c != nil {
				_ = c.Close()
			}
			lease.Release()
			return nil, err
		}
		if ctx.Err() != nil {
			_ = c.Close()
			lease.Release()
			return nil, ctx.Err()
		}
		return vpnroute.WrapConn(c, lease), nil
	}
	if !allowed {
		return dial(ctx, false)
	}
	key := vpn + "|" + network + "|" + net.JoinHostPort(ip.String(), port)
	return vpnroute.DialWithFallback(ctx, key,
		func(ctx context.Context) (net.Conn, error) { return dial(ctx, false) },
		func(ctx context.Context) (net.Conn, error) { return dial(ctx, true) })
}
