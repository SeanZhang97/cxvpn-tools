package outbound

import (
	"net"

	C "github.com/metacubex/mihomo/constant"
)

func (d *Direct) physicalPacketConn(pc net.PacketConn) C.PacketConn {
	// NewPacketConn retains ResolveUDP for later datagrams. Preserve Direct's
	// resolver and IP preference while reporting the socket's actual outlet.
	physical := *d
	base := *d.Base
	base.name = "PHYSICAL"
	physical.Base = &base
	return NewPacketConn(pc, &physical)
}
