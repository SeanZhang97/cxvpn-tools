package outbound

import (
	"context"
	"net"
	"net/netip"
	"testing"
	"time"

	"github.com/metacubex/mihomo/component/resolver"
	C "github.com/metacubex/mihomo/constant"
)

type physicalTestResolver struct {
	resolver.Resolver
	ip               netip.Addr
	calls, ipv6Calls int
}

func (*physicalTestResolver) Invalid() bool { return true }
func (r *physicalTestResolver) LookupIP(context.Context, string) ([]netip.Addr, error) {
	r.calls++
	return []netip.Addr{r.ip}, nil
}
func (r *physicalTestResolver) LookupIPv4(context.Context, string) ([]netip.Addr, error) {
	r.calls++
	return []netip.Addr{r.ip}, nil
}
func (r *physicalTestResolver) LookupIPv6(context.Context, string) ([]netip.Addr, error) {
	r.calls++
	r.ipv6Calls++
	return []netip.Addr{r.ip}, nil
}

type physicalTestPacket struct{}

func (*physicalTestPacket) ReadFrom([]byte) (int, net.Addr, error) { return 0, nil, net.ErrClosed }
func (*physicalTestPacket) WriteTo([]byte, net.Addr) (int, error)  { return 0, net.ErrClosed }
func (*physicalTestPacket) Close() error                           { return nil }
func (*physicalTestPacket) LocalAddr() net.Addr                    { return &net.UDPAddr{} }
func (*physicalTestPacket) SetDeadline(time.Time) error            { return nil }
func (*physicalTestPacket) SetReadDeadline(time.Time) error        { return nil }
func (*physicalTestPacket) SetWriteDeadline(time.Time) error       { return nil }

func TestCXVPNPhysicalPacketPreservesDirectDNSAndPreference(t *testing.T) {
	originalDefault, originalDirect := resolver.DefaultResolver, resolver.DirectHostResolver
	originalDisableIPv6 := resolver.DisableIPv6
	t.Cleanup(func() {
		resolver.DefaultResolver = originalDefault
		resolver.DirectHostResolver = originalDirect
		resolver.DisableIPv6 = originalDisableIPv6
	})
	resolver.DisableIPv6 = false
	defaultDNS := &physicalTestResolver{ip: netip.MustParseAddr("2001:4860::8888")}
	directDNS := &physicalTestResolver{ip: netip.MustParseAddr("2606:4700::1111")}
	resolver.DefaultResolver = defaultDNS
	resolver.DirectHostResolver = directDNS
	d := NewDirectWithOption(DirectOption{Name: "VPN-test", BasicOption: BasicOption{IPVersion: C.IPv6Only}})
	pc := d.physicalPacketConn(&physicalTestPacket{})
	defer pc.Close()
	if chains := pc.Chains(); len(chains) != 1 || chains[0] != "PHYSICAL" {
		t.Fatalf("wrong actual outlet: %v", chains)
	}
	if d.Name() != "VPN-test" {
		t.Fatal("physical labeling changed the shared VPN adapter")
	}
	for _, resolved := range []bool{false, true} {
		metadata := &C.Metadata{Host: "cxvpn-offline-test.invalid", DstPort: 443}
		if resolved {
			metadata.DstIP = defaultDNS.ip
		}
		if err := pc.ResolveUDP(context.Background(), metadata); err != nil {
			t.Fatal(err)
		}
		if metadata.DstIP != directDNS.ip {
			t.Fatalf("resolved=%v: Direct DNS bypassed, got %v", resolved, metadata.DstIP)
		}
	}
	if defaultDNS.calls != 0 || directDNS.calls != 2 || directDNS.ipv6Calls != 2 {
		t.Fatalf("resolver/preference changed: default=%d direct=%d ipv6=%d", defaultDNS.calls, directDNS.calls, directDNS.ipv6Calls)
	}
}
