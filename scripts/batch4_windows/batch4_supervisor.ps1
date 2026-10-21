<#
.SYNOPSIS
    يشغّل مشرف الاستيراد (الدفعة 4) على ويندوز.

.DESCRIPTION
    غلاف PowerShell حول scripts/batch4_windows/batch4_windows.py. نفس عقد
    batch4_supervisor.sh: يعيد التشغيل عند التوقّف النظيف، ويتوقّف عند
    الفشل أو عند stage=awaiting_cutover / failed.

    POSIX كما هو (المستورد يبقى متوقّفاً):
        powershell -ExecutionPolicy Bypass -File scripts\batch4_windows\batch4_supervisor.ps1 `
            -DataRoot C:\poetry-static

    خلفية (نافذة مخفية) عبر جدولة المهام:
        powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File scripts\batch4_windows\batch4_supervisor.ps1 `
            -DataRoot C:\poetry-static

.PARAMETER DataRoot
    مجلد poetry-static (الذي يحوي .d1-final و .cache).

.PARAMETER Python
    مسار مفسر بايثون. الافتراضي: python من PATH.

.PARAMETER MaxWrites
    سقف الكتابات لكل تشغيل (نفس --max-writes).

.PARAMETER PollSeconds
    فترة إعادة فحص الحصة (نفس --poll-seconds).

.PARAMETER D1Quota
    سقف الكتابة اليومي بالصفوف الذي يفترضه المشغّل.
    100000 = Workers Free (السقف اليومي المطبَّق) — 1000000 = Workers Paid (لا سقف يومي).
    الافتراضي 1000000 لأن حساب Cloudflare هنا على Workers Paid.

.PARAMETER ApplyPatches
    تطبيق تعديلات ويندوز على شجرة البيانات (مرة واحدة، قابلة للتراجع).

.PARAMETER DryRun
    خطة فقط بلا كتابة.

.EXAMPLE
    .\scripts\batch4_windows\batch4_supervisor.ps1 -DataRoot C:\poetry-static -ApplyPatches
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [string]$Python = "python",
    [int]$MaxWrites = 85000,
    [int]$PollSeconds = 600,
    [int]$D1Quota = 1000000,
    [int]$D1QuotaMargin = 5000,
    [switch]$ApplyPatches,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# سجلات ويندوز ليست UTF-8 افتراضياً؛ اجعل الطرفية UTF-8 قبل التشغيل.
try { chcp 65001 | Out-Null } catch { }
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$script = Join-Path $PSScriptRoot "batch4_windows.py"
if (-not (Test-Path $script)) {
    throw "launcher not found: $script"
}

$cliArgs = @($script, "--data-root", $DataRoot, "--max-writes", $MaxWrites, "--poll-seconds", $PollSeconds,
              "--d1-quota", $D1Quota, "--d1-quota-margin", $D1QuotaMargin)

if ($ApplyPatches) {
    $cliArgs += "--apply-patches"
    & $Python @cliArgs
    if ($LASTEXITCODE -ne 0) { throw "patching failed (exit $LASTEXITCODE)" }
    $cliArgs = @($script, "--data-root", $DataRoot, "--supervise", "--max-writes", $MaxWrites, "--poll-seconds", $PollSeconds,
                  "--d1-quota", $D1Quota, "--d1-quota-margin", $D1QuotaMargin)
}
elseif ($DryRun) {
    $cliArgs += "--dry-run"
}
else {
    $cliArgs += "--supervise"
}

Write-Host "== batch-4 supervisor (windows) ==" -ForegroundColor Cyan
Write-Host "  data-root : $DataRoot"
Write-Host "  python    : $Python"
Write-Host "  log       : $DataRoot\.d1-final\batch4_runner.log"
Write-Host "  d1-quota  : $D1Quota rows/day (margin $D1QuotaMargin)"
Write-Host ""

& $Python @cliArgs
exit $LASTEXITCODE
