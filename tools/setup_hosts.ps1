# 演示 hosts 映射工具(需管理员权限)
# 用法: 右键"以管理员身份运行" 或 在管理员 PowerShell 中:
#   powershell -ExecutionPolicy Bypass -File tools\setup_hosts.ps1
# 把四个演示域名指向 127.0.0.1,浏览器/curl 即可访问离线演示站点。
# 清理: tools\cleanup_hosts.ps1

$ErrorActionPreference = "Stop"
$hostsFile = "$env:SystemRoot\System32\drivers\etc\hosts"
$mapping = @(
    "127.0.0.1 corp-doc.com",
    "127.0.0.1 game-site.com",
    "127.0.0.1 shop.example",
    "127.0.0.1 blocked-site.example"
)

$lines = Get-Content $hostsFile
$added = 0
foreach ($entry in $mapping) {
    $domain = ($entry -split "\s+")[1]
    $pattern = '(?im)^\s*([0-9.]+|\:\:)\s+' + [regex]::Escape($domain) + '\s*$'
    $hit = $lines | Where-Object { $_ -match $pattern } | Select-Object -First 1
    if ($hit) {
        Write-Host "已存在: $entry  (当前指向 $hit)"
    } else {
        $lines += "# demo mapping (enterprise-proxy)"
        $lines += $entry
        $added++
        Write-Host "添加: $entry"
    }
}
if ($added -gt 0) {
    Set-Content -Path $hostsFile -Value $lines -Encoding ASCII
    Write-Host "hosts 已更新,共新增 $added 条映射。"
} else {
    Write-Host "无需修改:四个演示域名均已映射到 127.0.0.1。"
}
# 提示 DNS 缓存刷新
ipconfig /flushdns | Out-Null
Write-Host "DNS 缓存已刷新。"
