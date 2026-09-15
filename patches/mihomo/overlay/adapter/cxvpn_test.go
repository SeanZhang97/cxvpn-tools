package adapter

import "testing"

func TestCXVPNDirectCompatibility(t *testing.T) {
 for _, managed := range []bool{false, true} {
  config := map[string]any{"name": "直连e\u0301\U0001f1e8\U0001f1f3", "type": "direct", "interface-name": "VPN-test"}
  if managed { config["cxvpn-managed-route"] = true }
  if _, err := ParseProxy(config); err != nil { t.Fatalf("managed=%v: %v", managed, err) }
 }
}
