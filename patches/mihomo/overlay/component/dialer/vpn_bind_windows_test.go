package dialer

import (
	"context"
	"errors"
	"net"
	"net/netip"
	"syscall"
	"testing"
)

type managedRecordingRawConn struct {
	called bool
	err    error
}

func (r *managedRecordingRawConn) Control(func(uintptr)) error  { r.called = true; return r.err }
func (*managedRecordingRawConn) Read(func(uintptr) bool) error  { return nil }
func (*managedRecordingRawConn) Write(func(uintptr) bool) error { return nil }

func TestManagedBindingDoesNotSkipWildcardOrPrivateAddress(t *testing.T) {
	for _, tc := range []struct{ network, address, target string }{
		{"udp4", "0.0.0.0:0", "10.1.2.3"},
		{"udp6", "[::]:0", "fd00::1234"},
		{"tcp4", "10.1.2.3:443", "10.1.2.3"},
		{"tcp6", "[fd00::1234]:443", "fd00::1234"},
	} {
		t.Run(tc.network, func(t *testing.T) {
			marker := errors.New("control reached")
			raw := &managedRecordingRawConn{err: marker}
			err := managedBindControl(1, netip.MustParseAddr(tc.target))(context.Background(), tc.network, tc.address, raw)
			if !raw.called || !errors.Is(err, marker) {
				t.Fatalf("binding skipped: called=%v error=%v", raw.called, err)
			}
		})
	}
}

func TestManagedInterfaceResolutionObservesReconnect(t *testing.T) {
	index := 10
	lookup := func(string) (*net.Interface, error) { return &net.Interface{Index: index, Flags: net.FlagUp}, nil }
	first, err := managedInterfaceIndex("vpn", lookup)
	if err != nil {
		t.Fatal(err)
	}
	index = 20
	second, err := managedInterfaceIndex("vpn", lookup)
	if err != nil || first != 10 || second != 20 {
		t.Fatalf("stale interface: %d %d %v", first, second, err)
	}
	_, err = managedInterfaceIndex("vpn", func(string) (*net.Interface, error) { return &net.Interface{Index: 20}, nil })
	if err == nil {
		t.Fatal("down interface accepted")
	}
}

func TestManagedUDPWildcardHasInterfaceSocketOption(t *testing.T) {
	interfaces, err := net.Interfaces()
	if err != nil {
		t.Fatal(err)
	}
	var loopback *net.Interface
	for i := range interfaces {
		if interfaces[i].Flags&(net.FlagLoopback|net.FlagUp) == net.FlagLoopback|net.FlagUp {
			loopback = &interfaces[i]
			break
		}
	}
	if loopback == nil {
		t.Skip("no up loopback interface")
	}
	lc := &net.ListenConfig{}
	address, err := managedBindIfaceToListenConfig(loopback.Name, lc, "udp4", "0.0.0.0:0", netip.MustParseAddrPort("10.1.2.3:443"))
	if err != nil {
		t.Fatal(err)
	}
	packet, err := lc.ListenPacket(context.Background(), "udp4", address)
	if err != nil {
		t.Fatal(err)
	}
	defer packet.Close()
	raw, err := packet.(*net.UDPConn).SyscallConn()
	if err != nil {
		t.Fatal(err)
	}
	var value int
	var readErr error
	if err = raw.Control(func(fd uintptr) {
		value, readErr = syscall.GetsockoptInt(syscall.Handle(fd), syscall.IPPROTO_IP, IP_UNICAST_IF)
	}); err != nil {
		t.Fatal(err)
	}
	if readErr != nil {
		t.Fatal(readErr)
	}
	if value != loopback.Index {
		t.Fatalf("UDP socket is not bound: index=%d expected=%d", value, loopback.Index)
	}
}

func TestManagedUDPRejectsImplicitDualStackSocket(t *testing.T) {
	_, err := managedBindIfaceToListenConfig("unused", &net.ListenConfig{}, "udp", "", netip.MustParseAddrPort("10.1.2.3:443"))
	if err == nil {
		t.Fatal("implicit dual-stack UDP accepted")
	}
}
