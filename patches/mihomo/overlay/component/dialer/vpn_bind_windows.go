package dialer

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"syscall"
)

// Managed routes are prepared against the current Windows interface identity.
// Do not reuse Mihomo's interface cache after a VPN reconnect or adapter change.
func managedInterfaceIndex(name string, lookup func(string) (*net.Interface, error)) (int, error) {
	iface, err := lookup(name)
	if err != nil {
		return 0, fmt.Errorf("resolve managed interface: %w", err)
	}
	if iface == nil || iface.Index <= 0 || iface.Flags&net.FlagUp == 0 {
		return 0, errors.New("managed interface is unavailable")
	}
	return iface.Index, nil
}

func managedBindControl(index int, destination netip.Addr) controlFn {
	return func(ctx context.Context, network, _ string, raw syscall.RawConn) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		var bindErr error
		// A ListenPacket Control address is the local wildcard, not the remote
		// target. It must never suppress interface binding for UDP.
		controlErr := raw.Control(func(fd uintptr) {
			switch network {
			case "tcp4", "udp4":
				if !destination.Unmap().Is4() {
					bindErr = errors.New("managed socket address family mismatch")
					return
				}
				bindErr = bind4(syscall.Handle(fd), index)
			case "tcp6", "udp6":
				if !destination.Is6() || destination.Is4In6() {
					bindErr = errors.New("managed socket address family mismatch")
					return
				}
				bindErr = bind6(syscall.Handle(fd), index)
			default:
				bindErr = errors.New("managed socket requires an explicit address family")
			}
		})
		if controlErr != nil {
			return controlErr
		}
		return bindErr
	}
}

func managedBindIfaceToDialer(name string, d *net.Dialer, _ string, destination netip.Addr) error {
	index, err := managedInterfaceIndex(name, net.InterfaceByName)
	if err != nil {
		return err
	}
	addControlToDialer(d, managedBindControl(index, destination))
	return nil
}

func managedBindIfaceToListenConfig(name string, lc *net.ListenConfig, network, address string, target netip.AddrPort) (string, error) {
	// A single-family socket prevents a later UDP target from using the other,
	// unbound family. The caller selects udp4/udp6 from the prepared target.
	if network != "udp4" && network != "udp6" {
		return "", errors.New("managed UDP requires a single address family")
	}
	index, err := managedInterfaceIndex(name, net.InterfaceByName)
	if err != nil {
		return "", err
	}
	addControlToListenConfig(lc, managedBindControl(index, target.Addr()))
	return address, nil
}
