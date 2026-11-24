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

function Fail {
    param([string]$Message, [int]$Code = 1)
    # Write plain stderr text instead of Write-Error: under
    # $ErrorActionPreference='Stop' a Write-Error aborts the script with a
    # PowerShell ErrorRecord and stack trace before reaching a useful exit code.
    [Console]::Error.WriteLine($Message)
    exit $Code
}

$action = 'patch-files'
$rebuild = $false
$cliPath = ''
$positionalOnly = $false

for ($i = 0; $i -lt $args.Count; $i++) {
    $arg = $args[$i]
    if ($positionalOnly) {
        if ($cliPath) { [Console]::Error.WriteLine("Лишний аргумент: $arg"); Write-Usage; exit 2 }
        $cliPath = $arg
        continue
    }
    switch ($arg) {
        { $_ -in '-h', '--help', 'help' } { Write-Usage; exit 0 }
        { $_ -in '--status', 'status' } { $action = 'status'; continue }
        { $_ -in '--restore', 'restore' } { $action = 'restore-files'; continue }
        { $_ -in '--patch', 'patch' } { $action = 'patch-files'; continue }
        '--rebuild' { $rebuild = $true; continue }
        '--' { $positionalOnly = $true; continue }
        { $_ -like '-*' } {
            [Console]::Error.WriteLine("Неизвестная опция: $arg")
            Write-Usage
            exit 2
        }
        default {
            if ($cliPath) { [Console]::Error.WriteLine("Лишний аргумент: $arg"); Write-Usage; exit 2 }
            $cliPath = $arg
        }
    }
}

function Resolve-CliPath {
    # The Rust engine refuses to patch a symlink, so unwrap reparse points
    # (symlinks/junctions) and scoop .shim files to the real binary.
    param([string]$Path, [int]$Depth = 0)
    if ($Depth -gt 10) { return [IO.Path]::GetFullPath($Path) }
    $full = [IO.Path]::GetFullPath($Path)
    $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if (-not $item) { return $full }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -and $item.Target) {
        $target = [string](@($item.Target)[0])
        if (-not [IO.Path]::IsPathRooted($target)) {
            $target = Join-Path (Split-Path -Parent $full) $target
        }
        return Resolve-CliPath -Path $target -Depth ($Depth + 1)
    }
    # A scoop shim is a small loader exe; the real binary is in the sibling .shim file.
    $shim = [IO.Path]::ChangeExtension($full, '.shim')
    if (Test-Path -LiteralPath $shim -PathType Leaf) {
        $line = Get-Content -LiteralPath $shim -TotalCount 4 |
            Where-Object { $_ -match '^\s*path\s*=' } | Select-Object -First 1
        if ($line -match '^\s*path\s*=\s*"?([^"\r\n]+?)"?\s*$') {
            return Resolve-CliPath -Path $Matches[1] -Depth ($Depth + 1)
        }
    }
    return $full
}

function Test-NativeGoCli {
    # Mirrors native_cli() in src/core/detector.rs: parse the PE section table,
    # then look for the "\xff Go buildinf:" marker inside candidate sections.
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        try {
            $reader = New-Object System.IO.BinaryReader($stream)
            if ($reader.ReadUInt16() -ne 0x5A4D) { return $false } # 'MZ'
            $stream.Position = 0x3C
            $peOffset = $reader.ReadInt32()
            $stream.Position = $peOffset
            if ($reader.ReadUInt32() -ne 0x00004550) { return $false } # 'PE\0\0'
            $stream.Position = $peOffset + 4 + 2
            $sectionCount = $reader.ReadUInt16()
            $stream.Position = $peOffset + 4 + 8
            $symbolTableOffset = $reader.ReadUInt32()
            $symbolCount = $reader.ReadUInt32()
            $optHeaderSize = $reader.ReadUInt16()
            $sectionTable = $peOffset + 4 + 20 + $optHeaderSize

            # Long section names (>8 chars) are stored as "/offset" pointing
            # into the COFF string table after the symbol table.
            $stringTableOffset = $symbolTableOffset + 18 * $symbolCount

            $candidates = @()
            for ($i = 0; $i -lt $sectionCount; $i++) {
                $stream.Position = $sectionTable + 40 * $i
                $nameBytes = $reader.ReadBytes(8)
                $name = [Text.Encoding]::ASCII.GetString($nameBytes).TrimEnd([char]0)
                if ($name -match '^/(\d+)$' -and $stringTableOffset -gt 0) {
                    $saved = $stream.Position
                    try {
                        $stream.Position = $stringTableOffset + [int]$Matches[1]
                        $chars = New-Object System.Collections.Generic.List[byte]
                        while (($b = $reader.ReadByte()) -ne 0) { $chars.Add($b) }
                        $name = [Text.Encoding]::ASCII.GetString($chars.ToArray())
                    } catch { }
                    $stream.Position = $saved
                }
                if ($name -in '.go.buildinfo', '__go_buildinfo', '.data') {
                    $stream.Position = $sectionTable + 40 * $i + 16
                    $rawSize = $reader.ReadUInt32()
                    $rawOffset = $reader.ReadUInt32()
                    if ($rawSize -gt 0 -and $rawOffset -gt 0) {
                        $candidates += ,@($rawOffset, [Math]::Min($rawSize, 64KB))
                    }
                }
            }

            foreach ($candidate in $candidates) {
                $stream.Position = $candidate[0]
                $bytes = $reader.ReadBytes([int]$candidate[1])
                if (-not $bytes) { continue }
                # Latin1 maps bytes 1:1, so .IndexOf on the decoded string is
                # a native search over at most 64 KB instead of a PS loop.
                $text = [Text.Encoding]::GetEncoding(28591).GetString($bytes)
                $marker = "$([char]0xFF) Go buildinf:"
                $at = $text.IndexOf($marker, [StringComparison]::Ordinal)
                while ($at -ge 0) {
                    if ($at + 15 -lt $bytes.Length -and
                        ($bytes[$at + 14] -eq 4 -or $bytes[$at + 14] -eq 8) -and
                        ($bytes[$at + 15] -band (-bnot 3)) -eq 0) {
                        return $true
                    }
                    $at = $text.IndexOf($marker, $at + 1, [StringComparison]::Ordinal)
                }
            }
            return $false
        } finally {
            $stream.Dispose()
        }
    } catch {
        return $false
    }
}

function Test-MiseShim {
    param([string]$Path)
    $leaf = Split-Path -Leaf $Path
    return (($leaf -like 'mise*') -or ($Path -like '*\mise\shims\*'))
}

function Resolve-MiseShim {
    param([string]$ShimPath, [string]$Name)
    $toolName = [IO.Path]::GetFileNameWithoutExtension($Name)
    $miseExe = $null
    if ((Split-Path -Leaf $ShimPath) -like 'mise*') {
        $miseExe = $ShimPath
    } else {
        $cmd = Get-Command mise.exe -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd) { $miseExe = $cmd.Source }
    }
    if (-not $miseExe) { return $null }
    try { $candidate = (& $miseExe which $toolName 2>$null | Select-Object -First 1) } catch { return $null }
    if ($candidate -and (Test-Path -LiteralPath "$candidate" -PathType Leaf)) {
        $resolved = Resolve-CliPath "$candidate"
        if (-not (Test-MiseShim $resolved)) { return $resolved }
    }
    return $null
}

function Find-Cli {
    param([string]$Candidate)
    if ($Candidate) {
        if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            Fail "Файл не найден: $Candidate"
        }
        $resolved = Resolve-CliPath $Candidate
        if (Test-MiseShim $resolved) {
            $unwrapped = Resolve-MiseShim $resolved (Split-Path -Leaf $Candidate)
            if (-not $unwrapped) {
                Fail "Не удалось разрешить mise-shim: $Candidate"
            }
            return $unwrapped
        }
        return $resolved
    }

    # 1) PATH lookup: unwrap shims, then accept only a real native Go binary
    #    (skips .cmd/.ps1 wrappers and the IDE's Electron executable).
    foreach ($name in 'antigravity.exe', 'agy.exe', 'antigravity', 'agy') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandType -eq 'Application' } |
            Select-Object -First 1
        if (-not $cmd) { continue }
        $resolved = Resolve-CliPath $cmd.Source
        if (Test-MiseShim $resolved) {
            $resolved = Resolve-MiseShim $resolved $name
            if (-not $resolved) { continue }
        }
        if (Test-NativeGoCli $resolved) { return $resolved }
    }

    # 2) Known install locations (mirrors the engine detector + mise/scoop layouts).
    $userHome = [Environment]::GetFolderPath('UserProfile')
    $local = $env:LOCALAPPDATA
    $fallbacks = @(
        "$local\mise\installs\antigravity-cli\latest\bin\agy.exe",
        "$local\mise\installs\antigravity-cli\latest\bin\antigravity.exe",
        "$local\mise\installs\antigravity-cli\latest\agy.exe",
        "$local\mise\installs\antigravity-cli\latest\antigravity.exe",
        "$local\mise\installs\antigravity-cli\global\bin\agy.exe",
        "$local\mise\installs\antigravity-cli\global\agy.exe",
        "$local\mise\shims\agy.exe",
        "$local\mise\shims\antigravity.exe",
        "$local\agy\agy.exe",
        "$local\agy\antigravity.exe",
        "$local\Programs\Antigravity CLI\agy.exe",
        "$local\Programs\antigravity-cli\agy.exe",
        "$local\Programs\Antigravity\agy.exe",
        "$userHome\.gemini\antigravity-cli\bin\agy.exe",
        "$userHome\.gemini\antigravity-cli\bin\antigravity.exe",
        "$userHome\.gemini\bin\agy.exe",
        "$userHome\.agy\bin\agy.exe",
        "$userHome\.agy\bin\antigravity.exe",
        "$userHome\scoop\apps\agy\current\agy.exe",
        "$userHome\scoop\apps\antigravity-cli\current\agy.exe",
        "$userHome\scoop\apps\antigravity-cli\current\antigravity.exe",
        "$userHome\scoop\shims\agy.exe",
        "$userHome\scoop\shims\antigravity.exe"
    )
    foreach ($item in $fallbacks) {
        if (Test-Path -LiteralPath $item -PathType Leaf) {
            $resolved = Resolve-CliPath $item
            if (Test-MiseShim $resolved) {
                $resolved = Resolve-MiseShim $resolved (Split-Path -Leaf $item)
                if (-not $resolved) { continue }
            }
            if (Test-NativeGoCli $resolved) { return $resolved }
        }
    }

    Fail 'Antigravity CLI не найден. Передайте путь к исполняемому файлу аргументом.'
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
        Fail 'Rust/Cargo не найден. Установите rustup (https://rustup.rs) и повторите запуск.'
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
