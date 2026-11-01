# Foreground inventory ONLY. No installation, process launch, task inspection,
# credential access, WMI/CIM, network, persistent writes, or authority grants.
# Run explicitly with -NoProfile -NonInteractive. Never dot-source on import.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$probeClock = [System.Diagnostics.Stopwatch]::StartNew()
try {
    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        throw 'unsupported platform'
    }
    # Only bounded in-process properties. No host/user name, SID, path,
    # command line, scheduler arguments, serial number or secret is emitted.
    $inventory = [ordered]@{
        schema = 'director.host-inventory.v1'
        provenance = 'REAL_HOST_PROBE'
        platform = 'Windows'
        os_build = [System.Environment]::OSVersion.Version.Build
        logical_processors = [System.Environment]::ProcessorCount
        powershell_major = $PSVersionTable.PSVersion.Major
        process_64bit = [System.Environment]::Is64BitProcess
    }
    $outputJson = ConvertTo-Json -InputObject $inventory -Compress -Depth 2
    if ($probeClock.Elapsed.TotalSeconds -gt 2 -or
        [System.Text.Encoding]::UTF8.GetByteCount($outputJson) -gt 4096) {
        throw 'probe bound exceeded'
    }
    [Console]::Out.WriteLine($outputJson)
} catch {
    # Exception details may contain private host data. Never print them.
    [Console]::Error.WriteLine('Inventory unavailable; UNQUALIFIED_HOST; NO_GO')
    exit 1
} finally {
    $probeClock.Stop()
}
