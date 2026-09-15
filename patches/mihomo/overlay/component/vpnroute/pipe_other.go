//go:build !windows

package vpnroute

import ("context";"errors")
func exchangeNative(context.Context,request)(string,error){return "",errors.New("managed VPN routes require Windows")}
