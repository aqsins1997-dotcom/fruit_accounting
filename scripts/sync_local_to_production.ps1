param(
    [string]$ProductionDatabaseUrl = $env:DATABASE_URL,
    [string]$SourceFixture = "",
    [switch]$PrepareOnly,
    [switch]$NoFlush,
    [switch]$SkipEnsureAdmin,
    [string]$AdminUsername = $(if ($env:FRUIT_ADMIN_USERNAME) { $env:FRUIT_ADMIN_USERNAME } else { "admin" }),
    [string]$AdminPassword = $(if ($env:FRUIT_ADMIN_PASSWORD) { $env:FRUIT_ADMIN_PASSWORD } else { "admin" }),
    [string]$RenderApiKey = $env:RENDER_API_KEY,
    [string]$RenderServiceId = $env:RENDER_SERVICE_ID,
    [string]$RenderDeployHookUrl = $env:RENDER_DEPLOY_HOOK_URL,
    [string]$VerifyBaseUrl = $env:FRUIT_VERIFY_BASE_URL
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$pythonCommand = if (Test-Path $venvPython) { $venvPython } else { "python" }
$backupRoot = Join-Path $env:USERPROFILE "Documents\fruit_accounting_backups"
$dailyDir = Join-Path $backupRoot (Get-Date -Format "yyyy-MM-dd")
$timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"

$tempSqlitePath = Join-Path $dailyDir "sync_source_$timestamp.sqlite3"
$cleanFixturePath = Join-Path $dailyDir "production_fixture_$timestamp.json"
$sourceSummaryPath = Join-Path $dailyDir "source_summary_$timestamp.json"
$productionBackupPath = Join-Path $dailyDir "production_backup_before_import_$timestamp.json"
$productionSummaryPath = Join-Path $dailyDir "production_summary_after_import_$timestamp.json"

function Restore-EnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowNull()][string]$Value
    )

    if ($null -eq $Value) {
        Remove-Item "Env:\$Name" -ErrorAction SilentlyContinue
    }
    else {
        Set-Item "Env:\$Name" $Value
    }
}

function ConvertTo-SqliteUrl {
    param([Parameter(Mandatory = $true)][string]$Path)
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    return "sqlite:///" + ($fullPath -replace "\\", "/")
}

function Invoke-Django {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    & $pythonCommand manage.py @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Django command failed: manage.py $($Arguments -join ' ')"
    }
}

function Invoke-DjangoCapture {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $output = & $pythonCommand manage.py @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Django command failed: manage.py $($Arguments -join ' ')"
    }
    return ($output -join [Environment]::NewLine)
}

function Invoke-Python {
    param([Parameter(Mandatory = $true)][string]$Code)

    $Code | & $pythonCommand -
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed."
    }
}

function Invoke-PythonCapture {
    param([Parameter(Mandatory = $true)][string]$Code)

    $output = $Code | & $pythonCommand -
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed."
    }
    return ($output -join [Environment]::NewLine)
}

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Assert-Equal {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowNull()]$Expected,
        [AllowNull()]$Actual
    )

    if ("$Expected" -ne "$Actual") {
        throw "Verification failed for $Name. Expected '$Expected', got '$Actual'."
    }
}

function Join-Url {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$Path
    )
    return $BaseUrl.TrimEnd("/") + $Path
}

function Test-WebLoginAndPurchasePage {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$Username,
        [Parameter(Mandatory = $true)][string]$Password
    )

    $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    $loginUrl = Join-Url -BaseUrl $BaseUrl -Path "/accounts/login/"
    $purchaseUrl = Join-Url -BaseUrl $BaseUrl -Path "/purchases/add/"

    $loginPage = Invoke-WebRequest -Uri $loginUrl -WebSession $session -UseBasicParsing -TimeoutSec 45
    $csrfToken = $null
    if ($loginPage.Content -match 'name=["'']csrfmiddlewaretoken["''][^>]*value=["'']([^"'']+)["'']') {
        $csrfToken = $Matches[1]
    }
    elseif ($loginPage.Content -match 'value=["'']([^"'']+)["''][^>]*name=["'']csrfmiddlewaretoken["'']') {
        $csrfToken = $Matches[1]
    }

    if (-not $csrfToken) {
        throw "Could not find CSRF token on login page."
    }

    $body = @{
        username = $Username
        password = $Password
        csrfmiddlewaretoken = $csrfToken
    }

    Invoke-WebRequest -Uri $loginUrl -Method Post -Body $body -WebSession $session -UseBasicParsing -MaximumRedirection 5 -TimeoutSec 45 | Out-Null
    $purchasePage = Invoke-WebRequest -Uri $purchaseUrl -WebSession $session -UseBasicParsing -MaximumRedirection 5 -TimeoutSec 45

    if ($purchasePage.Content -match "/accounts/login/") {
        throw "Login did not stick; purchase page redirected to login."
    }
    if (($purchasePage.Content -notmatch 'name=["'']supplier["'']') -or ($purchasePage.Content -notmatch 'name=["'']store["'']') -or ($purchasePage.Content -notmatch 'name=["'']product["'']')) {
        throw "New purchase page opened, but expected supplier/store/product fields were not found."
    }

    Write-Host "Login and new purchase page verified: $purchaseUrl" -ForegroundColor Green
}

function Invoke-RenderRestart {
    if ($RenderApiKey -and $RenderServiceId) {
        Write-Step "Restart Render service through Render API"
        $headers = @{
            Accept = "application/json"
            Authorization = "Bearer $RenderApiKey"
        }
        $restartUrl = "https://api.render.com/v1/services/$RenderServiceId/restart"
        Invoke-WebRequest -Uri $restartUrl -Method Post -Headers $headers -UseBasicParsing -TimeoutSec 60 | Out-Null
        Write-Host "Render restart requested for service $RenderServiceId." -ForegroundColor Green
        return
    }

    if ($RenderDeployHookUrl) {
        Write-Step "Trigger Render deploy hook"
        Invoke-WebRequest -Uri $RenderDeployHookUrl -Method Post -UseBasicParsing -TimeoutSec 60 | Out-Null
        Write-Host "Render deploy hook requested." -ForegroundColor Green
        return
    }

    Write-Warning "Render restart skipped. Set RENDER_API_KEY + RENDER_SERVICE_ID, or set RENDER_DEPLOY_HOOK_URL."
}

$summaryCode = @'
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

import json
from django.db.models import Sum
from apps.core.models import Customer, Product, Seller, Store, Supplier
from apps.inventory.models import Purchase, PurchaseItem, StoreStock
from apps.sales.models import Sale, SaleItem

def as_text(value):
    return None if value is None else str(value)

stocks = []
for row in StoreStock.objects.select_related("store", "product").order_by("store__name", "product__name"):
    stocks.append({
        "store": row.store.name,
        "product": row.product.name,
        "quantity_kg": as_text(row.quantity_kg),
        "average_purchase_price": as_text(row.average_purchase_price),
    })

summary = {
    "suppliers_count": Supplier.objects.count(),
    "stores_count": Store.objects.count(),
    "products_count": Product.objects.count(),
    "customers_count": Customer.objects.count(),
    "sellers_count": Seller.objects.count(),
    "purchases_count": Purchase.objects.count(),
    "purchase_items_count": PurchaseItem.objects.count(),
    "sales_count": Sale.objects.count(),
    "sale_items_count": SaleItem.objects.count(),
    "stock_rows_count": StoreStock.objects.count(),
    "stock_total_kg": as_text(StoreStock.objects.aggregate(total=Sum("quantity_kg"))["total"]),
    "suppliers": list(Supplier.objects.order_by("name").values_list("name", flat=True)),
    "stores": list(Store.objects.order_by("name").values_list("name", flat=True)),
    "products": list(Product.objects.order_by("name").values_list("name", flat=True)),
    "stocks": stocks,
}

print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
'@

$ensureAdminCode = @'
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

from django.contrib.auth import get_user_model

username = os.environ["FRUIT_SYNC_ADMIN_USERNAME"]
password = os.environ["FRUIT_SYNC_ADMIN_PASSWORD"]

User = get_user_model()
user, _ = User.objects.get_or_create(username=username)
user.is_active = True
user.is_staff = True
user.is_superuser = True
user.set_password(password)
user.save()
print(f"Admin user ready: {username}")
'@

$oldDatabaseUrl = $env:DATABASE_URL
$oldPythonUtf8 = $env:PYTHONUTF8
$oldPythonIoEncoding = $env:PYTHONIOENCODING
$oldSyncAdminUsername = $env:FRUIT_SYNC_ADMIN_USERNAME
$oldSyncAdminPassword = $env:FRUIT_SYNC_ADMIN_PASSWORD

New-Item -ItemType Directory -Path $dailyDir -Force | Out-Null

try {
    Push-Location $projectRoot

    if (-not $SourceFixture) {
        $defaultFixture = Join-Path $projectRoot "data.json"
        if (Test-Path $defaultFixture) {
            $SourceFixture = $defaultFixture
        }
        else {
            throw "SourceFixture is not set and data.json was not found."
        }
    }

    $SourceFixture = [System.IO.Path]::GetFullPath($SourceFixture)
    if (-not (Test-Path $SourceFixture)) {
        throw "Source fixture not found: $SourceFixture"
    }

    Write-Step "Prepare clean fixture from local source"
    Write-Host "Project: $projectRoot"
    Write-Host "Source fixture: $SourceFixture"
    Write-Host "Temporary SQLite DB: $tempSqlitePath"

    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:DATABASE_URL = ConvertTo-SqliteUrl -Path $tempSqlitePath

    if (Test-Path $tempSqlitePath) {
        Remove-Item -LiteralPath $tempSqlitePath -Force
    }

    Invoke-Django @("migrate", "--noinput")
    Invoke-Django @("loaddata", $SourceFixture)
    Invoke-Django @(
        "dumpdata",
        "--natural-foreign",
        "--natural-primary",
        "--exclude", "contenttypes",
        "--exclude", "auth.permission",
        "--exclude", "auth.group",
        "--exclude", "admin.logentry",
        "--exclude", "sessions.session",
        "--indent", "2",
        "--output", $cleanFixturePath
    )

    $sourceSummary = Invoke-PythonCapture $summaryCode
    $sourceSummary | Set-Content -LiteralPath $sourceSummaryPath -Encoding utf8
    Write-Host $sourceSummary
    Write-Host "Clean fixture saved: $cleanFixturePath" -ForegroundColor Green

    if ($PrepareOnly) {
        Write-Host ""
        Write-Host "PrepareOnly is set. Production database was not touched." -ForegroundColor Yellow
        return
    }

    if (-not $ProductionDatabaseUrl) {
        throw "ProductionDatabaseUrl is empty. Set DATABASE_URL or pass -ProductionDatabaseUrl."
    }
    if ($ProductionDatabaseUrl -notmatch "^postgres(ql)?://") {
        throw "ProductionDatabaseUrl must be a PostgreSQL URL. Refusing to import into: $ProductionDatabaseUrl"
    }

    Write-Step "Connect to production database and run migrations"
    $env:DATABASE_URL = $ProductionDatabaseUrl
    Invoke-Django @("migrate", "--noinput")

    Write-Step "Backup production database before import"
    Invoke-Django @(
        "dumpdata",
        "--natural-foreign",
        "--natural-primary",
        "--indent", "2",
        "--output", $productionBackupPath
    )
    Write-Host "Production backup saved: $productionBackupPath" -ForegroundColor Green

    if ($NoFlush) {
        Write-Warning "NoFlush is set. Existing production rows will remain, so duplicate/PK conflicts are possible."
    }
    else {
        Write-Step "Flush production tables"
        Invoke-Django @("flush", "--noinput")
        Invoke-Django @("migrate", "--noinput")
    }

    Write-Step "Load clean fixture into production"
    Invoke-Django @("loaddata", $cleanFixturePath)

    if (-not $SkipEnsureAdmin) {
        Write-Step "Ensure admin login exists"
        $env:FRUIT_SYNC_ADMIN_USERNAME = $AdminUsername
        $env:FRUIT_SYNC_ADMIN_PASSWORD = $AdminPassword
        Invoke-Python $ensureAdminCode
    }

    Write-Step "Recalculate production cash registers"
    Invoke-Django @("recalculate_cash")

    Write-Step "Run Django checks"
    Invoke-Django @("check")

    Write-Step "Verify production data"
    $productionSummary = Invoke-PythonCapture $summaryCode
    $productionSummary | Set-Content -LiteralPath $productionSummaryPath -Encoding utf8
    Write-Host $productionSummary

    $sourceObject = Get-Content -LiteralPath $sourceSummaryPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $productionObject = Get-Content -LiteralPath $productionSummaryPath -Raw -Encoding UTF8 | ConvertFrom-Json

    Assert-Equal "suppliers_count" $sourceObject.suppliers_count $productionObject.suppliers_count
    Assert-Equal "stores_count" $sourceObject.stores_count $productionObject.stores_count
    Assert-Equal "products_count" $sourceObject.products_count $productionObject.products_count
    Assert-Equal "purchases_count" $sourceObject.purchases_count $productionObject.purchases_count
    Assert-Equal "purchase_items_count" $sourceObject.purchase_items_count $productionObject.purchase_items_count
    Assert-Equal "stock_rows_count" $sourceObject.stock_rows_count $productionObject.stock_rows_count
    Assert-Equal "stock_total_kg" $sourceObject.stock_total_kg $productionObject.stock_total_kg

    $sourceStocks = @($sourceObject.stocks) | ConvertTo-Json -Depth 5 -Compress
    $productionStocks = @($productionObject.stocks) | ConvertTo-Json -Depth 5 -Compress
    Assert-Equal "stocks" $sourceStocks $productionStocks

    Write-Host "Production data matches the local source fixture." -ForegroundColor Green

    Invoke-RenderRestart

    if ($VerifyBaseUrl) {
        Write-Step "Verify login and new purchase page"
        $lastError = $null
        for ($attempt = 1; $attempt -le 18; $attempt++) {
            try {
                Test-WebLoginAndPurchasePage -BaseUrl $VerifyBaseUrl -Username $AdminUsername -Password $AdminPassword
                $lastError = $null
                break
            }
            catch {
                $lastError = $_
                Write-Host "Web check attempt $attempt failed: $($_.Exception.Message)" -ForegroundColor Yellow
                Start-Sleep -Seconds 10
            }
        }
        if ($lastError) {
            throw $lastError
        }
    }
    else {
        Write-Warning "Web verification skipped. Set FRUIT_VERIFY_BASE_URL, for example https://fruit-accounting.onrender.com"
    }

    Write-Host ""
    Write-Host "Sync completed successfully." -ForegroundColor Green
}
finally {
    Pop-Location
    Restore-EnvValue -Name "DATABASE_URL" -Value $oldDatabaseUrl
    Restore-EnvValue -Name "PYTHONUTF8" -Value $oldPythonUtf8
    Restore-EnvValue -Name "PYTHONIOENCODING" -Value $oldPythonIoEncoding
    Restore-EnvValue -Name "FRUIT_SYNC_ADMIN_USERNAME" -Value $oldSyncAdminUsername
    Restore-EnvValue -Name "FRUIT_SYNC_ADMIN_PASSWORD" -Value $oldSyncAdminPassword
}
