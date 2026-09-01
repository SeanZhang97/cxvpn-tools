# -*- coding: utf-8 -*-
"""Windows EAP-MSCHAPv2 凭据写入与无界面 RAS 连接。"""
import base64
import re

from . import vpn_os


_EAP_PS = r"""
$ErrorActionPreference = 'Stop'
$Entry = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String($env:CXVPN_EAP_ENTRY_B64))
$User = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String($env:CXVPN_EAP_USER_B64))
$Password = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String($env:CXVPN_EAP_PASSWORD_B64))
$ShouldDial = $env:CXVPN_EAP_DIAL -eq '1'
$Pbk = Join-Path $env:APPDATA `
    'Microsoft\Network\Connections\Pbk\rasphone.pbk'

if (-not ('CxVpnEap.Native' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace CxVpnEap {
    [ComImport, Guid("2933BF86-7B36-11d2-B20E-00C04F983E60"),
     InterfaceType(ComInterfaceType.InterfaceIsDual)]
    public interface IXMLDOMElement {}

    [StructLayout(LayoutKind.Sequential)]
    public struct EAP_METHOD_TYPE {
        public byte type;
        public uint dwVendorId;
        public uint dwVendorType;
        public uint dwAuthorId;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct RASEAPUSERIDENTITY {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 257)]
        public string szUserName;
        public uint dwSizeofEapInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct RASEAPINFO {
        public uint dwSizeofEapInfo;
        public IntPtr pbEapInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct RASDEVSPECIFICINFO {
        public uint dwSize;
        public IntPtr pbDevSpecificInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct RASDIALEXTENSIONS {
        public uint dwSize;
        public uint dwfOptions;
        public IntPtr hwndParent;
        public UIntPtr reserved;
        public UIntPtr reserved1;
        public RASEAPINFO RasEapInfo;
        public int fSkipPppAuth;
        public RASDEVSPECIFICINFO RasDevSpecificInfo;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct RASDIALPARAMS {
        public uint dwSize;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 257)]
        public string szEntryName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 129)]
        public string szPhoneNumber;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 129)]
        public string szCallbackNumber;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 257)]
        public string szUserName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 257)]
        public string szPassword;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 16)]
        public string szDomain;
        public uint dwSubEntry;
        public UIntPtr dwCallbackId;
        public uint dwIfIndex;
    }

    public static class Native {
        [DllImport("eappcfg.dll")]
        public static extern uint EapHostPeerConfigXml2Blob(
            uint flags, IntPtr doc, out uint size, out IntPtr blob,
            out EAP_METHOD_TYPE method, out IntPtr error);

        [DllImport("eappcfg.dll")]
        public static extern uint EapHostPeerCredentialsXml2Blob(
            uint flags, IntPtr doc, uint configSize, IntPtr config,
            out uint size, out IntPtr blob, out EAP_METHOD_TYPE method,
            out IntPtr error);

        [DllImport("eappcfg.dll")]
        public static extern void EapHostPeerFreeMemory(IntPtr data);

        [DllImport("eappcfg.dll")]
        public static extern void EapHostPeerFreeErrorMemory(IntPtr error);

        [DllImport("rasapi32.dll", CharSet = CharSet.Unicode)]
        public static extern uint RasSetEapUserData(
            IntPtr token, string phonebook, string entry,
            IntPtr data, uint size);

        [DllImport("rasapi32.dll", CharSet = CharSet.Unicode)]
        public static extern uint RasGetEapUserIdentity(
            string phonebook, string entry, uint flags, IntPtr hwnd,
            out IntPtr identity);

        [DllImport("rasapi32.dll")]
        public static extern void RasFreeEapUserIdentity(IntPtr identity);

        [DllImport("rasapi32.dll", CharSet = CharSet.Unicode)]
        public static extern uint RasDial(
            ref RASDIALEXTENSIONS extensions, string phonebook,
            ref RASDIALPARAMS parameters, uint notifierType,
            IntPtr notifier, out IntPtr connection);
    }
}
'@
}

function Invoke-CxVpnEap {
    $configNode = [IntPtr]::Zero
    $credentialNode = [IntPtr]::Zero
    $configBlob = [IntPtr]::Zero
    $credentialBlob = [IntPtr]::Zero
    $identity = [IntPtr]::Zero
    $configError = [IntPtr]::Zero
    $credentialError = [IntPtr]::Zero
    try {
        $profile = Get-VpnConnection -Name $Entry -ErrorAction Stop
        $configXml = $profile.EapConfigXmlStream.OuterXml
        if ([string]::IsNullOrWhiteSpace($configXml)) {
            return 'CXVPN_EAP|NOT_EAP|0|'
        }

        $escapedUser = [Security.SecurityElement]::Escape($User)
        $escapedPassword = [Security.SecurityElement]::Escape($Password)
        $credentialXml = @"
<?xml version="1.0"?>
<EapHostUserCredentials xmlns="http://www.microsoft.com/provisioning/EapHostUserCredentials" xmlns:eapCommon="http://www.microsoft.com/provisioning/EapCommon" xmlns:baseEap="http://www.microsoft.com/provisioning/BaseEapMethodUserCredentials">
  <EapMethod><eapCommon:Type>26</eapCommon:Type><eapCommon:AuthorId>0</eapCommon:AuthorId></EapMethod>
  <Credentials xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:baseEap="http://www.microsoft.com/provisioning/BaseEapUserPropertiesV1" xmlns:MsChapV2="http://www.microsoft.com/provisioning/MsChapV2UserPropertiesV1">
    <baseEap:Eap><baseEap:Type>26</baseEap:Type><MsChapV2:EapType><MsChapV2:Username>$escapedUser</MsChapV2:Username><MsChapV2:Password>$escapedPassword</MsChapV2:Password><MsChapV2:LogonDomain></MsChapV2:LogonDomain></MsChapV2:EapType></baseEap:Eap>
  </Credentials>
</EapHostUserCredentials>
"@

        $configDocument = New-Object -ComObject Msxml2.DOMDocument.6.0
        $configDocument.async = $false
        $configDocument.resolveExternals = $false
        if (-not $configDocument.loadXML($configXml)) {
            return 'CXVPN_EAP|CONFIG_XML|13|'
        }
        $credentialDocument = New-Object -ComObject Msxml2.DOMDocument.6.0
        $credentialDocument.async = $false
        $credentialDocument.resolveExternals = $false
        if (-not $credentialDocument.loadXML($credentialXml)) {
            return 'CXVPN_EAP|CREDENTIAL_XML|13|'
        }

        $interfaceType = [type][CxVpnEap.IXMLDOMElement]
        $configNode = [Runtime.InteropServices.Marshal]::GetComInterfaceForObject(
            $configDocument.documentElement, $interfaceType)
        $credentialNode = [Runtime.InteropServices.Marshal]::GetComInterfaceForObject(
            $credentialDocument.documentElement, $interfaceType)

        $configSize = [uint32]0
        $configMethod = New-Object CxVpnEap.EAP_METHOD_TYPE
        $code = [CxVpnEap.Native]::EapHostPeerConfigXml2Blob(
            0, $configNode, [ref]$configSize, [ref]$configBlob,
            [ref]$configMethod, [ref]$configError)
        if ($code -ne 0) {
            return "CXVPN_EAP|CONFIG|$code|"
        }
        if ($configMethod.type -ne 26) {
            return "CXVPN_EAP|UNSUPPORTED|$($configMethod.type)|"
        }

        $credentialSize = [uint32]0
        $credentialMethod = New-Object CxVpnEap.EAP_METHOD_TYPE
        $code = [CxVpnEap.Native]::EapHostPeerCredentialsXml2Blob(
            0, $credentialNode, $configSize, $configBlob,
            [ref]$credentialSize, [ref]$credentialBlob,
            [ref]$credentialMethod, [ref]$credentialError)
        if ($code -ne 0) {
            return "CXVPN_EAP|CREDENTIALS|$code|"
        }

        $code = [CxVpnEap.Native]::RasSetEapUserData(
            [IntPtr]::Zero, $Pbk, $Entry, $credentialBlob, $credentialSize)
        if ($code -ne 0) {
            return "CXVPN_EAP|STORE|$code|"
        }

        # 2 = RASEAPF_NonInteractive。成功即证明连接时不需要 EAP UI。
        $code = [CxVpnEap.Native]::RasGetEapUserIdentity(
            $Pbk, $Entry, 2, [IntPtr]::Zero, [ref]$identity)
        if ($code -ne 0) {
            return "CXVPN_EAP|IDENTITY|$code|"
        }
        if (-not $ShouldDial) {
            return 'CXVPN_EAP|PREPARED|0|'
        }

        $identityHeader = [Runtime.InteropServices.Marshal]::PtrToStructure(
            $identity, [type][CxVpnEap.RASEAPUSERIDENTITY])
        $eapOffset = [Runtime.InteropServices.Marshal]::SizeOf(
            [type][CxVpnEap.RASEAPUSERIDENTITY])
        $parameters = New-Object CxVpnEap.RASDIALPARAMS
        $parameters.dwSize = [Runtime.InteropServices.Marshal]::SizeOf($parameters)
        $parameters.szEntryName = $Entry
        $parameters.szUserName = $identityHeader.szUserName
        $extensions = New-Object CxVpnEap.RASDIALEXTENSIONS
        $extensions.dwSize = [Runtime.InteropServices.Marshal]::SizeOf($extensions)
        $eapInfo = New-Object CxVpnEap.RASEAPINFO
        $eapInfo.dwSizeofEapInfo = $identityHeader.dwSizeofEapInfo
        $eapInfo.pbEapInfo = [IntPtr]::Add($identity, $eapOffset)
        $extensions.RasEapInfo = $eapInfo
        $connection = [IntPtr]::Zero
        $code = [CxVpnEap.Native]::RasDial(
            [ref]$extensions, $Pbk, [ref]$parameters, 0,
            [IntPtr]::Zero, [ref]$connection)
        return "CXVPN_EAP|DIAL|$code|"
    } catch {
        return "CXVPN_EAP|EXCEPTION|1|$($_.Exception.Message)"
    } finally {
        if ($identity -ne [IntPtr]::Zero) {
            [CxVpnEap.Native]::RasFreeEapUserIdentity($identity)
        }
        if ($configNode -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::Release($configNode) | Out-Null
        }
        if ($credentialNode -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::Release($credentialNode) | Out-Null
        }
        if ($configBlob -ne [IntPtr]::Zero) {
            [CxVpnEap.Native]::EapHostPeerFreeMemory($configBlob)
        }
        if ($credentialBlob -ne [IntPtr]::Zero) {
            [CxVpnEap.Native]::EapHostPeerFreeMemory($credentialBlob)
        }
        if ($configError -ne [IntPtr]::Zero) {
            [CxVpnEap.Native]::EapHostPeerFreeErrorMemory($configError)
        }
        if ($credentialError -ne [IntPtr]::Zero) {
            [CxVpnEap.Native]::EapHostPeerFreeErrorMemory($credentialError)
        }
    }
}

Invoke-CxVpnEap
"""


def _encoded(value):
    return base64.b64encode(str(value or '').encode('utf-8')).decode('ascii')


def _invoke(name, username, password, dial=False, timeout=90):
    env = {
        'CXVPN_EAP_ENTRY_B64': _encoded(name),
        'CXVPN_EAP_USER_B64': _encoded(username),
        'CXVPN_EAP_PASSWORD_B64': _encoded(password),
        'CXVPN_EAP_DIAL': '1' if dial else '0',
    }
    try:
        ok, out, err = vpn_os._ps(_EAP_PS, timeout=timeout, env=env)
    except Exception as exc:
        return True, False, f'EAP 系统调用失败: {exc}'
    if not ok:
        return True, False, err or out or 'EAP 系统调用失败'
    match = re.search(
        r'^CXVPN_EAP\|([^|]+)\|(\d+)\|(.*)$', out or '', re.MULTILINE)
    if not match:
        return True, False, out or 'EAP 系统调用未返回结果'
    stage, raw_code, detail = match.groups()
    code = int(raw_code)
    if stage == 'NOT_EAP':
        return False, True, ''
    if stage == 'PREPARED':
        return True, True, 'EAP 凭据已写入 Windows'
    if stage == 'DIAL' and code == 0:
        return True, True, '已使用软件保存的 EAP 凭据连接'
    if stage == 'IDENTITY' and code == 703:
        return True, False, 'EAP 凭据无法用于无界面连接 (703)'
    if stage == 'DIAL':
        return True, False, f'RAS 错误 {code}'
    suffix = f': {detail}' if detail else ''
    return True, False, f'EAP {stage} 错误 {code}{suffix}'


def prepare(name, username, password):
    """写入 EAP 用户数据；非 EAP 条目返回 handled=False。"""
    return _invoke(name, username, password, dial=False, timeout=45)


def connect(name, username, password, timeout=90):
    """写入 EAP 用户数据并通过 RasDialW 无界面连接。"""
    return _invoke(name, username, password, dial=True, timeout=timeout)
