# 本脚本在发布机从源码构建版本化镜像，可按需只构建后端、前端或全部组件并推送到镜像仓库。
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("all", "backend", "frontend")]
    [string]$Component = "all",

    # 版本标签统一使用发布日期：backend-YYYY-MM-DD；同一天多次发布追加 -2、-3。
    [string]$Version = ("backend-" + (Get-Date -Format "yyyy-MM-dd")),

    [string]$Registry = "",

    [switch]$Push,

    [switch]$NoCache
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$registryPrefix = $Registry.Trim().TrimEnd("/")
if ($registryPrefix) {
    $registryPrefix = "$registryPrefix/"
}

$backendImage = "${registryPrefix}jira-automation/backend:$Version"
$frontendVersion = if ($Version -match "^backend-") {
    $Version -replace "^backend-", "frontend-"
} else {
    "frontend-$Version"
}
$frontendImage = "${registryPrefix}jira-automation/frontend:$frontendVersion"

function Invoke-DockerCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    Write-Host "docker $($Arguments -join ' ')"
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker 命令执行失败，退出码：$LASTEXITCODE"
    }
}

function Push-DockerImage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Image
    )

    Write-Host "docker push $Image"
    & docker push $Image
    if ($LASTEXITCODE -ne 0) {
        throw "镜像推送失败，退出码：$LASTEXITCODE"
    }
}

function Build-BackendImage {
    $arguments = @("build", "--progress=plain", "--target", "runtime", "--tag", $backendImage)
    if ($NoCache) {
        $arguments += "--no-cache"
    }
    $arguments += (Join-Path $projectRoot "backend")
    Invoke-DockerCommand -Arguments $arguments
    if ($Push) {
        Push-DockerImage -Image $backendImage
    }
}

function Build-FrontendImage {
    $arguments = @("build", "--progress=plain", "--tag", $frontendImage)
    if ($NoCache) {
        $arguments += "--no-cache"
    }
    $arguments += (Join-Path $projectRoot "frontend")
    Invoke-DockerCommand -Arguments $arguments
    if ($Push) {
        Push-DockerImage -Image $frontendImage
    }
}

if ($Component -in @("all", "backend")) {
    Build-BackendImage
}
if ($Component -in @("all", "frontend")) {
    Build-FrontendImage
}

Write-Host "发布镜像处理完成。"
if ($Component -in @("all", "backend")) {
    Write-Host "BACKEND_IMAGE=$backendImage"
}
if ($Component -in @("all", "frontend")) {
    Write-Host "FRONTEND_IMAGE=$frontendImage"
}
