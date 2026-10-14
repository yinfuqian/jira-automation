# 本脚本只打包生产运行所需的 Compose、环境模板、用户表示例和说明，不包含任何应用源码。
[CmdletBinding()]
param(
    [string]$Version = (Get-Date -Format "yyyyMMdd-HHmmss"),

    [string]$OutputDirectory = "release"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$outputPath = if ([IO.Path]::IsPathRooted($OutputDirectory)) {
    [IO.Path]::GetFullPath($OutputDirectory)
} else {
    [IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory))
}
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null

$staging = [IO.Path]::GetFullPath((Join-Path $outputPath "production-deploy-$Version"))
if (-not $staging.StartsWith($outputPath, [StringComparison]::OrdinalIgnoreCase)) {
    throw "临时打包目录超出指定输出目录，已拒绝操作：$staging"
}
if (Test-Path -LiteralPath $staging) {
    Remove-Item -LiteralPath $staging -Recurse -Force
}
New-Item -ItemType Directory -Path $staging | Out-Null

# 白名单是生产部署包的安全边界，禁止改为复制整个仓库或任何业务源码目录。
Copy-Item -LiteralPath (Join-Path $projectRoot "docker-compose-prod.yaml") -Destination $staging
Copy-Item -LiteralPath (Join-Path $projectRoot "restart-master.sh") -Destination $staging
Copy-Item -LiteralPath (Join-Path $projectRoot "deploy/.env.production.example") -Destination $staging
Copy-Item -LiteralPath (Join-Path $projectRoot "deploy/users.example.csv") -Destination $staging
Copy-Item -LiteralPath (Join-Path $projectRoot "deploy/README.md") -Destination $staging

# Maven 设置随包分发：生产 Compose 把它挂载为 /root/.m2/settings.xml（只含内网镜像地址，无凭据）。
# 缺少该文件时 Docker 会把挂载点建成空目录，Maven 读不到镜像配置。
$mavenConfigSource = Join-Path $projectRoot "config/maven/settings.xml"
$mavenConfigTarget = Join-Path $staging "config/maven"
New-Item -ItemType Directory -Force -Path $mavenConfigTarget | Out-Null
Copy-Item -LiteralPath $mavenConfigSource -Destination $mavenConfigTarget

# 技能定义随部署包一起发布；技能令牌等本地文件不打包。
Copy-Item -LiteralPath (Join-Path $projectRoot "skills") -Destination $staging -Recurse

$stagedSkills = Join-Path $staging "skills"
if (Test-Path -LiteralPath $stagedSkills) {
    $tokenFiles = Get-ChildItem -LiteralPath $stagedSkills -Recurse -Force |
        Where-Object { $_.Name -in @(".jira-token", ".jira-token.txt", "jira-token.txt") }
    foreach ($tokenFile in $tokenFiles) {
        Remove-Item -LiteralPath $tokenFile.FullName -Force
    }
}

$archivePath = Join-Path $outputPath "jira-automation-production-deploy-$Version.zip"
if (Test-Path -LiteralPath $archivePath) {
    Remove-Item -LiteralPath $archivePath -Force
}
Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $archivePath -CompressionLevel Optimal
Remove-Item -LiteralPath $staging -Recurse -Force

Write-Host "生产部署包已生成：$archivePath"
Write-Host "部署包仅包含运行配置，不包含 backend、frontend、windows-client 或其他源码。"
