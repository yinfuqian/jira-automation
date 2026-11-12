# 将 jira-gate-1 及其依赖技能整合为可安装的本地预检 skill。
# 产物目录为 skills/jira-gate-1-local，凭据与运行态一律不复制。
param(
    [string]$Name = "jira-gate-1-local"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$SkillsRoot = Join-Path $RepoRoot "skills"
$Target = Join-Path $SkillsRoot $Name

if (Test-Path -LiteralPath $Target) {
    throw "目标目录已存在：$Target"
}
New-Item -ItemType Directory -Path $Target | Out-Null
Copy-Item -Path (Join-Path $SkillsRoot "jira-gate-1/*") -Destination $Target -Recurse -Force

New-Item -ItemType Directory -Path (Join-Path $Target "subskills") | Out-Null
foreach ($Dependency in @("review-jira-req", "review-jira-design", "review-jira-qa")) {
    Copy-Item -Path (Join-Path $SkillsRoot $Dependency) -Destination (Join-Path $Target "subskills/$Dependency") -Recurse
}

Get-ChildItem -LiteralPath $Target -Recurse -Force -Directory |
    Where-Object { $_.Name -in @("__pycache__", "dist", ".qa", ".git", "node_modules") } |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $Target -Recurse -Force -File |
    Where-Object { $_.Name -in @(".DS_Store", ".jira-token", ".jira-token.txt", "jira-token.txt") } |
    Remove-Item -Force

Write-Host "已生成技能目录：$Target"
