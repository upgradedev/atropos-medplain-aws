param(
    [Parameter(Mandatory = $true)][string]$Plan,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [string]$VoiceName = 'Microsoft Zira Desktop - English (United States)'
)
$ErrorActionPreference = 'Stop'
$planPath = (Resolve-Path -LiteralPath $Plan).Path
$definition = Get-Content -LiteralPath $planPath -Raw | ConvertFrom-Json
if ($definition.schema -ne 'medplain-narrated-walkthrough-v1') { throw 'Unsupported narration schema' }
if (Test-Path -LiteralPath $OutputDirectory) { throw 'Preserve the prior narration output' }
$null = New-Item -ItemType Directory -Path $OutputDirectory
$outputRoot = (Resolve-Path -LiteralPath $OutputDirectory).Path
$voice = New-Object -ComObject SAPI.SpVoice
$selected = @($voice.GetVoices() | Where-Object { $_.GetDescription() -eq $VoiceName })
if ($selected.Count -ne 1) { throw 'The exact local voice is unavailable' }
$voice.Voice = $selected[0]
$voice.Rate = 0
$voice.Volume = 100
$receipts = @()
foreach ($beat in $definition.beats) {
    if ($beat.id -notmatch '^[a-z][a-z0-9_]{0,23}$') { throw 'Unsafe beat identifier' }
    $wavePath = Join-Path $outputRoot ($beat.id + '.wav')
    if (Test-Path -LiteralPath $wavePath) { throw 'Existing narration cannot be overwritten' }
    $stream = New-Object -ComObject SAPI.SpFileStream
    $stream.Format.Type = 22
    try {
        $stream.Open($wavePath, 3, $false)
        # All speech is directed into this WAV stream. No playback call exists.
        $voice.AudioOutputStream = $stream
        $null = $voice.Speak([string]$beat.narration, 0)
    }
    finally {
        $stream.Close()
    }
    $receipts += [ordered]@{
        beat = $beat.id
        path = $wavePath
        sha256 = (Get-FileHash -LiteralPath $wavePath -Algorithm SHA256).Hash.ToLowerInvariant()
        bytes = (Get-Item -LiteralPath $wavePath).Length
    }
}
$proof = [ordered]@{
    status = 'local_narration_files_created_without_playback'
    voice = $VoiceName
    voice_rate = 0
    source_plan_sha256 = (Get-FileHash -LiteralPath $planPath -Algorithm SHA256).Hash.ToLowerInvariant()
    generator = 'Existing Windows SAPI; no model or network call'
    audible_playback = $false
    files = $receipts
}
$proof | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $outputRoot 'narration-proof.json') -Encoding utf8
$proof | ConvertTo-Json -Depth 8
