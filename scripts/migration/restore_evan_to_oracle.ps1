[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Archive,
    [Parameter(Mandatory)][string]$Destination,
    [string]$HostName = '146.181.24.74',
    [string]$UserName = 'ubuntu',
    [string]$SshKeyPath = 'E:\Oracle\SSH\ssh-key-2026-08-21.key',
    [string]$RecoveryKeyPath = 'E:\Gale-codex\gale-bot-vps\recovery-user\recovery-key.dpapi'
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security
Add-Type -AssemblyName System.Security.Cryptography.Pkcs

if ($HostName -notmatch '^[A-Za-z0-9.-]+$' -or $UserName -notmatch '^[a-z_][a-z0-9_-]*$') {
    throw 'Invalid SSH target.'
}
if ($Destination -notmatch '^/var/lib/evan-ombre-rehearsal/[0-9]{8}T[0-9]{6}Z$') {
    throw 'Restore destination must be a timestamped child of /var/lib/evan-ombre-rehearsal.'
}

$resolvedArchive = (Resolve-Path -LiteralPath $Archive).Path
$resolvedSshKey = (Resolve-Path -LiteralPath $SshKeyPath).Path
$resolvedRecoveryKey = (Resolve-Path -LiteralPath $RecoveryKeyPath).Path
$checksumPath = "$resolvedArchive.sha256"
if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
    throw 'Matching checksum file is missing.'
}

$archiveName = [IO.Path]::GetFileName($resolvedArchive)
$checksumLine = (Get-Content -Raw -LiteralPath $checksumPath).Trim()
if ($checksumLine -notmatch '^([0-9a-fA-F]{64})  (evan-ombre-[0-9]{8}T[0-9]{6}Z\.tar\.gz\.cms)$') {
    throw 'Invalid checksum format.'
}
if ($Matches[2] -ne $archiveName) {
    throw 'Checksum filename mismatch.'
}
$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $resolvedArchive).Hash.ToLowerInvariant()
if ($actualHash -ne $Matches[1].ToLowerInvariant()) {
    throw 'Encrypted archive checksum mismatch.'
}

$protectedKey = [IO.File]::ReadAllBytes($resolvedRecoveryKey)
$privateKey = [System.Security.Cryptography.ProtectedData]::Unprotect(
    $protectedKey,
    $null,
    [System.Security.Cryptography.DataProtectionScope]::CurrentUser
)
$rsa = [System.Security.Cryptography.RSA]::Create()
$read = 0
$rsa.ImportPkcs8PrivateKey($privateKey, [ref]$read)
$cms = [System.Security.Cryptography.Pkcs.EnvelopedCms]::new()
$cms.Decode([IO.File]::ReadAllBytes($resolvedArchive))
$cms.Decrypt($cms.RecipientInfos[0], $rsa)

$tempSshKey = Join-Path $env:TEMP ("evan-ombre-ssh-{0}.key" -f [guid]::NewGuid().ToString('N'))
try {
    Copy-Item -LiteralPath $resolvedSshKey -Destination $tempSshKey
    icacls $tempSshKey /inheritance:r /grant:r "$($env:USERNAME):(F)" | Out-Null

    $partial = "/var/lib/evan-ombre-rehearsal/.$([IO.Path]::GetFileName($Destination)).partial"
    $remoteCommand = "sudo -n sh -c 'set -eu; umask 077; test ! -e `"`$1`"; test ! -e `"`$2`"; install -d -o root -g root -m 0700 /var/lib/evan-ombre-rehearsal; install -d -o root -g root -m 0700 `"`$2`"; tar -xzf - -C `"`$2`" --no-same-owner --no-same-permissions; mv -- `"`$2`" `"`$1`"' sh '$Destination' '$partial'"

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = 'ssh'
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in @(
        '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=yes',
        '-o', 'ConnectTimeout=20',
        '-i', $tempSshKey,
        "$UserName@$HostName",
        $remoteCommand
    )) {
        $startInfo.ArgumentList.Add($argument)
    }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw 'Failed to start SSH restore process.'
    }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $content = $cms.ContentInfo.Content
    $process.StandardInput.BaseStream.Write($content, 0, $content.Length)
    $process.StandardInput.BaseStream.Flush()
    $process.StandardInput.Close()
    $process.WaitForExit()
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    if ($process.ExitCode -ne 0) {
        throw "Oracle restore extraction failed with exit code $($process.ExitCode): $stderr"
    }
    if ($stdout.Trim()) {
        Write-Verbose $stdout.Trim()
    }
    Write-Output "PASS: encrypted archive checksum verified and decrypted in memory; restored to isolated Oracle destination; sha256=$actualHash"
} finally {
    if ($privateKey) {
        [Array]::Clear($privateKey, 0, $privateKey.Length)
    }
    if ($cms -and $cms.ContentInfo -and $cms.ContentInfo.Content) {
        [Array]::Clear($cms.ContentInfo.Content, 0, $cms.ContentInfo.Content.Length)
    }
    if ($rsa) {
        $rsa.Dispose()
    }
    Remove-Item -LiteralPath $tempSshKey -Force -ErrorAction SilentlyContinue
}
