// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package onvifdisc

import (
	"encoding/xml"
	"net"
	"strings"
	"testing"
)

// A Hikvision-style reply, trimmed: prefixes differ per vendor, which is why
// the parser matches local names only.
const hikReply = `<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
 xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<env:Header><wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action></env:Header>
<env:Body><d:ProbeMatches><d:ProbeMatch>
<wsa:EndpointReference><wsa:Address>urn:uuid:3fa1fe68-b915-4053-a3e1-c0a8010a1001</wsa:Address></wsa:EndpointReference>
<d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>
<d:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/HIKVISION%20DS-2CD2143G2-I onvif://www.onvif.org/hardware/DS-2CD2143G2-I onvif://www.onvif.org/location/city/hangzhou</d:Scopes>
<d:XAddrs>http://192.168.1.64/onvif/device_service http://[fe80::1]/onvif/device_service</d:XAddrs>
<d:MetadataVersion>10</d:MetadataVersion>
</d:ProbeMatch></d:ProbeMatches></env:Body></env:Envelope>`

// A Dahua/CP Plus-style reply on a non-default port, with underscores in
// the name scope.
const dahuaReply = `<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
<s:Body><d:ProbeMatches><d:ProbeMatch>
<a:EndpointReference><a:Address>uuid:5f5a69c2-e0ae-504f-829b-00ef8f1a2b3c</a:Address></a:EndpointReference>
<d:Scopes>onvif://www.onvif.org/name/CP_PLUS_Gate onvif://www.onvif.org/hardware/CP-UNC-TA21L3</d:Scopes>
<d:XAddrs>http://192.168.1.108:8088/onvif/device_service</d:XAddrs>
</d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>`

func TestProbeIsValidXML(t *testing.T) {
	msg := ProbeMessage(probeTypes[0])
	var v struct{}
	if err := xml.Unmarshal(msg, &v); err != nil {
		t.Fatalf("probe is not XML: %v", err)
	}
	if !strings.Contains(string(msg), "NetworkVideoTransmitter") || !strings.Contains(string(msg), "uuid:") {
		t.Fatal("probe misses its type or message id")
	}
}

func TestParsesHikvisionStyleReply(t *testing.T) {
	ds := ParseProbeMatches([]byte(hikReply), net.ParseIP("192.168.1.64"))
	if len(ds) != 1 {
		t.Fatalf("got %+v", ds)
	}
	d := ds[0]
	if d.IP != "192.168.1.64" || d.Port != 80 || d.Scheme != "http" {
		t.Fatalf("address: %+v", d)
	}
	if d.Name != "HIKVISION DS-2CD2143G2-I" || d.Hardware != "DS-2CD2143G2-I" || d.Location != "city/hangzhou" {
		t.Fatalf("scopes: %+v", d)
	}
	if len(d.XAddrs) != 1 {
		t.Fatalf("only the IPv4 address it answered from is kept: %+v", d.XAddrs)
	}
}

func TestParsesAPortAndUnderscoredName(t *testing.T) {
	ds := ParseProbeMatches([]byte(dahuaReply), net.ParseIP("192.168.1.108"))
	if len(ds) != 1 || ds[0].Port != 8088 || ds[0].Name != "CP PLUS Gate" {
		t.Fatalf("got %+v", ds)
	}
}

func TestPrefersTheAddressItAnsweredFrom(t *testing.T) {
	reply := strings.Replace(dahuaReply,
		"http://192.168.1.108:8088/onvif/device_service",
		"http://10.0.0.5/onvif/device_service http://192.168.1.108:8088/onvif/device_service", 1)
	ds := ParseProbeMatches([]byte(reply), net.ParseIP("192.168.1.108"))
	if len(ds) != 1 || ds[0].IP != "192.168.1.108" || ds[0].Port != 8088 {
		t.Fatalf("got %+v", ds)
	}
}

func TestGarbageIsIgnored(t *testing.T) {
	if ds := ParseProbeMatches([]byte("not xml"), nil); ds != nil {
		t.Fatalf("got %+v", ds)
	}
	if ds := ParseProbeMatches([]byte(`<Envelope><Body><Hello/></Body></Envelope>`), nil); len(ds) != 0 {
		t.Fatalf("got %+v", ds)
	}
}

// An HP printer that answered a camera probe on this test network, with its
// own WS-Discovery service and no ONVIF type.
const printerReply = `<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery">
<SOAP-ENV:Body><wsd:ProbeMatches><wsd:ProbeMatch>
<wsa:EndpointReference><wsa:Address>urn:uuid:22b39183-93e4-4960-87f1-3d5f78f3830b</wsa:Address></wsa:EndpointReference>
<wsd:Types>wsdp:Device wprt:PrintDeviceType</wsd:Types>
<wsd:XAddrs>http://192.168.31.121:3911/ http://[FE80::FAED:FCFF:FE93:188B]:3911/</wsd:XAddrs>
</wsd:ProbeMatch></wsd:ProbeMatches></SOAP-ENV:Body></SOAP-ENV:Envelope>`

func TestAPrinterIsNotACamera(t *testing.T) {
	if ds := ParseProbeMatches([]byte(printerReply), net.ParseIP("192.168.31.121")); len(ds) != 0 {
		t.Fatalf("a printer was offered as a camera: %+v", ds)
	}
}

// The real CP PLUS recorder on this test network: no Types element the
// parser relies on, but an ONVIF device service.
func TestARecorderWithOnlyAnOnvifAddressIsACamera(t *testing.T) {
	reply := strings.Replace(dahuaReply, "http://192.168.1.108:8088/onvif/device_service", "http://192.168.31.155/onvif/device_service", 1)
	ds := ParseProbeMatches([]byte(reply), net.ParseIP("192.168.31.155"))
	if len(ds) != 1 || ds[0].IP != "192.168.31.155" {
		t.Fatalf("got %+v", ds)
	}
}

// ── Replies are anyone's UDP on the LAN ──

func TestAReplyCannotListACameraUnderAnotherHost(t *testing.T) {
	// Sent from .50, claiming to be the camera at .64: the entry is .50.
	ds := ParseProbeMatches([]byte(hikReply), net.ParseIP("192.168.1.50"))
	if len(ds) != 1 || ds[0].IP != "192.168.1.50" || len(ds[0].XAddrs) != 0 {
		t.Fatalf("a reply planted another host: %+v", ds)
	}
}

func TestReplyFieldsAreBoundedAndPrintable(t *testing.T) {
	long := strings.Repeat("A", 5000)
	reply := strings.Replace(dahuaReply, "onvif://www.onvif.org/name/CP_PLUS_Gate",
		"onvif://www.onvif.org/name/"+long+" onvif://www.onvif.org/hardware/x%0Ay%07z", 1)
	ds := ParseProbeMatches([]byte(reply), net.ParseIP("192.168.1.108"))
	if len(ds) != 1 {
		t.Fatalf("got %+v", ds)
	}
	if n := len([]rune(ds[0].Name)); n != maxFieldLen {
		t.Fatalf("name not clipped: %d runes", n)
	}
	if strings.ContainsAny(ds[0].Hardware, "\n\a") {
		t.Fatalf("control characters kept: %q", ds[0].Hardware)
	}
}

func TestBadPortsSchemesAndTooManyAddressesAreDropped(t *testing.T) {
	addrs := "ftp://192.168.1.108/onvif/device_service http://192.168.1.108:99999/onvif/device_service"
	for i := 0; i < 10; i++ {
		addrs += " http://192.168.1.108:8088/onvif/device_service"
	}
	reply := strings.Replace(dahuaReply, "http://192.168.1.108:8088/onvif/device_service", addrs, 1)
	ds := ParseProbeMatches([]byte(reply), net.ParseIP("192.168.1.108"))
	if len(ds) != 1 || ds[0].Scheme != "http" || ds[0].Port != 8088 || len(ds[0].XAddrs) != maxXAddrs {
		t.Fatalf("got %+v", ds)
	}
}

func TestNoSourceAddressNoDevice(t *testing.T) {
	if ds := ParseProbeMatches([]byte(dahuaReply), nil); ds != nil {
		t.Fatalf("got %+v", ds)
	}
}
