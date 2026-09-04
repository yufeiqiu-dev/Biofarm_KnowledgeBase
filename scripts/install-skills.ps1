<#
.SYNOPSIS
    Install skills from this knowledge base into a project's .claude/skills/.

.DESCRIPTION
    This repo stores each skill's parts in three folders (skills/, scripts/,
    documentation/) so they are easy to browse by kind. Claude Code, however,
    loads a skill as one self-contained directory. This script bridges the two:
    for each skill it copies

        skills/<name>/SKILL.md      -> .claude/skills/<name>/SKILL.md
        scripts/<name>/*            -> .claude/skills/<name>/scripts/
        documentation/<name>/*      -> .claude/skills/<name>/references/

    which is exactly the layout SKILL.md's relative paths assume.

    Re-running replaces the installed copy, so this is also how you pick up
    updates after a git pull. Edit the source in this repo, never the installed
    copy - the next run overwrites it.

.PARAMETER Target
    Project root to install into. Defaults to the parent of this repo, which is
    the workspace holding Biofarm_Backend and Biofarm_Frontend.

.PARAMETER Skill
    Install only this skill. Default: all of them.

.EXAMPLE
    ./scripts/install-skills.ps1
    ./scripts/install-skills.ps1 -Target C:\path\to\workspace -Skill local-setup
#>
param(
    [string]$Target,
    [string]$Skill = "*"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Target) { $Target = Split-Path -Parent $RepoRoot }
$Target = (Resolve-Path $Target).Path

Write-Host "Knowledge base : $RepoRoot"
Write-Host "Installing into: $Target\.claude\skills`n"

$skillDirs = Get-ChildItem -Path (Join-Path $RepoRoot "skills") -Directory |
             Where-Object { $_.Name -like $Skill }

if (-not $skillDirs) {
    throw "No skills matched '$Skill' under $RepoRoot\skills"
}

foreach ($dir in $skillDirs) {
    $name = $dir.Name
    $dest = Join-Path $Target ".claude\skills\$name"

    if (-not (Test-Path (Join-Path $dir.FullName "SKILL.md"))) {
        Write-Warning "$name has no SKILL.md - skipping"
        continue
    }

    # Replace rather than merge, so a file deleted upstream does not linger here.
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null

    Copy-Item (Join-Path $dir.FullName "*") -Destination $dest -Recurse -Force
    Write-Host "  + $name/SKILL.md"

    $scriptSrc = Join-Path $RepoRoot "scripts\$name"
    if (Test-Path $scriptSrc) {
        $scriptDest = Join-Path $dest "scripts"
        New-Item -ItemType Directory -Force -Path $scriptDest | Out-Null
        Copy-Item (Join-Path $scriptSrc "*") -Destination $scriptDest -Recurse -Force
        # Compiled bytecode from a local test run is noise in an installed copy.
        Get-ChildItem -Path $scriptDest -Recurse -Directory -Filter "__pycache__" |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        $n = (Get-ChildItem -Path $scriptDest -Recurse -File).Count
        Write-Host "  + $name/scripts ($n files)"
    }

    $docSrc = Join-Path $RepoRoot "documentation\$name"
    if (Test-Path $docSrc) {
        $docDest = Join-Path $dest "references"
        New-Item -ItemType Directory -Force -Path $docDest | Out-Null
        Copy-Item (Join-Path $docSrc "*") -Destination $docDest -Recurse -Force
        $n = (Get-ChildItem -Path $docDest -Recurse -File).Count
        Write-Host "  + $name/references ($n files)"
    }
}

Write-Host "`nDone. Start a new Claude Code session in $Target to pick up the skills."
