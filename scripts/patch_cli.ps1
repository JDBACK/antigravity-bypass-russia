# File-only CLI patching, status and rollback for Windows.
# Resolves agy.exe / antigravity.exe from PATH, mise/scoop/npm installs and
# known directories, then forwards patch-files / restore-files / status to the
# Rust engine (target/release/antigravity-bypass-russia.exe).
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new() } catch {}

function Write-Usage {
    @"
Использование:
  .\scripts\patch_cli.ps1 [ОПЦИИ] [путь к antigravity.exe или agy.exe]

Режимы:
  (по умолчанию)      Найти Antigravity CLI и применить файловый патч (patch-files)
  --restore, restore  Восстановить оригинальный бинарник CLI из бэкапа (restore-files)
  --status, status    Показать текущий статус патча CLI и системы
  --rebuild           Принудительно пересобрать Rust-движок перед запуском
  -h, --help          Показать эту справку
"@
}

$action = 'patch-files'
$rebuild = $false
$cliPath = ''

foreach ($arg in $args) {
    switch ($arg) {
        { $_ -in '-h', '--help', 'help' } { Write-Usage; exit 0 }
        { $_ -in '--status', 'status' } { $action = 'status'; continue }
        { $_ -in '--restore', 'restore' } { $action = 'restore-files'; continue }
        { $_ -in '--patch', 'patch' } { $action = 'patch-files'; continue }
        '--rebuild' { $rebuild = $true; continue }
        '--' { continue }
        { $_ -like '-*' } {
            Write-Error "Неизвестная опция: $arg"
            Write-Usage
            exit 2
        }
        default {
            if ($cliPath) {
                Write-Error "Лишний аргумент: $arg"
                Write-Usage
                exit 2
            }
            $cliPath = $arg
        }
    }
}

function Resolve-CliPath {
    param([string]$Path)
    # Resolve-Path resolves to a canonical absolute path; .NET resolves any
    # directory junctions/symlinks along the way.
    return (Resolve-Path -LiteralPath $Path).Path
}

function Test-NativeGoCli {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        try {
            $reader = New-Object System.IO.BinaryReader($stream)
            # A native CLI is a PE image: MZ header, PE signature at e_lfanew.
            if ($reader.ReadUInt16() -ne 0x5A4D) { return $false } # 'MZ'
            $stream.Position = 0x3C
            $peOffset = $reader.ReadInt32()
            $stream.Position = $peOffset
            if ($reader.ReadUInt32() -ne 0x00004550) { return $false } # 'PE\0\0'
            # Go binaries carry a buildinfo section. Its PE name may live in
            # the COFF string table, so scan the file for the marker bytes
            # (".go.buildinfo" / "Go buildinf") in 4 MB chunks instead.
            $markers = @(
                [Text.Encoding]::ASCII.GetBytes('.go.buildinfo'),
                [Text.Encoding]::ASCII.GetBytes('Go buildinf')
            )
            $tail = New-Object byte[] 0
            $buffer = New-Object byte[] 4MB
            while (($read = $reader.Read($buffer, 0, $buffer.Length)) -gt 0) {
                # Prepend the tail so a marker split across chunks is still found.
                $scan = New-Object byte[] ($tail.Length + $read)
                [Array]::Copy($tail, 0, $scan, 0, $tail.Length)
                [Array]::Copy($buffer, 0, $scan, $tail.Length, $read)
                foreach ($marker in $markers) {
                    for ($i = 0; $i -le $scan.Length - $marker.Length; $i++) {
                        $match = $true
                        for ($j = 0; $j -lt $marker.Length; $j++) {
                            if ($scan[$i + $j] -ne $marker[$j]) { $match = $false; break }
                        }
                        if ($match) { return $true }
                    }
                }
                $tailLength = [Math]::Min(16, $scan.Length)
                $tail = New-Object byte[] $tailLength
                [Array]::Copy($scan, $scan.Length - $tailLength, $tail, 0, $tailLength)
            }
            return $false
        } finally {
            $stream.Dispose()
        }
    } catch {
        return $false
    }
}

function Find-Cli {
    param([string]$Candidate)
    if ($Candidate) {
        if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            Write-Error "Файл не найден: $Candidate"
            exit 1
        }
        return (Resolve-CliPath $Candidate)
    }

    # 1) PATH lookup: only accept a real PE binary, skip .cmd/.ps1 shims.
    foreach ($name in 'antigravity.exe', 'agy.exe', 'antigravity', 'agy') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandType -eq 'Application' } |
            Select-Object -First 1
        if ($cmd -and (Test-NativeGoCli $cmd.Source)) {
            return $cmd.Source
        }
    }

    # 2) Known install locations (mirrors engine detector + mise/scoop layouts).
    $userHome = [Environment]::GetFolderPath('UserProfile')
    $local = $env:LOCALAPPDATA
    $fallbacks = @(
        "$local\mise\installs\antigravity-cli\latest\bin\agy.exe",
        "$local\mise\installs\antigravity-cli\latest\bin\antigravity.exe",
        "$local\mise\installs\antigravity-cli\latest\agy.exe",
        "$local\mise\installs\antigravity-cli\latest\antigravity.exe",
        "$local\mise\installs\antigravity-cli\global\bin\agy.exe",
        "$local\mise\installs\antigravity-cli\global\agy.exe",
        "$local\agy\agy.exe",
        "$local\agy\antigravity.exe",
        "$local\Programs\Antigravity CLI\agy.exe",
        "$local\Programs\antigravity-cli\agy.exe",
        "$local\Programs\Antigravity\agy.exe",
        "$env:USERPROFILE\.gemini\antigravity-cli\bin\agy.exe",
        "$env:USERPROFILE\.gemini\antigravity-cli\bin\antigravity.exe",
        "$env:USERPROFILE\.gemini\bin\agy.exe",
        "$env:USERPROFILE\.agy\bin\agy.exe",
        "$env:USERPROFILE\.agy\bin\antigravity.exe",
        "$env:USERPROFILE\scoop\apps\agy\current\agy.exe",
        "$env:USERPROFILE\scoop\apps\antigravity-cli\current\agy.exe",
        "$env:USERPROFILE\scoop\apps\antigravity-cli\current\antigravity.exe",
        "$env:USERPROFILE\scoop\shims\agy.exe",
        "$env:USERPROFILE\scoop\shims\antigravity.exe"
    )
    foreach ($item in $fallbacks) {
        if ((Test-Path -LiteralPath $item -PathType Leaf) -and (Test-NativeGoCli $item)) {
            return (Resolve-CliPath $item)
        }
    }

    Write-Error 'Antigravity CLI не найден. Передайте путь к исполняемому файлу аргументом.'
    exit 1
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoDir = Split-Path -Parent $scriptDir
$engine = Join-Path $repoDir 'target\release\antigravity-bypass-russia.exe'

function Test-NeedsBuild {
    if ($rebuild -or -not (Test-Path -LiteralPath $engine -PathType Leaf)) { return $true }
    $engineTime = (Get-Item -LiteralPath $engine).LastWriteTimeUtc
    $sources = @(
        (Join-Path $repoDir 'Cargo.toml'),
        (Join-Path $repoDir 'Cargo.lock'),
        (Join-Path $repoDir 'build.rs')
    ) + @(Get-ChildItem (Join-Path $repoDir 'src') -Recurse -File -ErrorAction SilentlyContinue |
          ForEach-Object { $_.FullName })
    foreach ($sourceFile in $sources) {
        if ((Get-Item -LiteralPath $sourceFile -ErrorAction SilentlyContinue) -and
            ((Get-Item -LiteralPath $sourceFile).LastWriteTimeUtc -gt $engineTime)) {
            return $true
        }
    }
    return $false
}

function Find-Cargo {
    $cmd = Get-Command cargo.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    $cargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { "$env:USERPROFILE\.cargo" }
    $candidate = Join-Path $cargoHome 'bin\cargo.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    $candidate = "$env:USERPROFILE\.cargo\bin\cargo.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    return $null
}

if (Test-NeedsBuild) {
    $cargo = Find-Cargo
    if (-not $cargo) {
        Write-Error 'Rust/Cargo не найден. Установите rustup (https://rustup.rs) и повторите запуск.'
        exit 1
    }
    $cargoDir = Split-Path -Parent $cargo
    $env:PATH = "$cargoDir;$env:PATH"
    & $cargo build --release --locked --manifest-path (Join-Path $repoDir 'Cargo.toml')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($action -eq 'status') {
    & $engine 'status'
    exit $LASTEXITCODE
}

$resolvedCli = Find-Cli $cliPath
& $engine $action $resolvedCli
exit $LASTEXITCODE
