# Load .env file
Get-Content ".env" | ForEach-Object {
    if ($_ -match "^\s*([^#][^=]+?)\s*=\s*(.+?)\s*$") {
        [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2])
    }
}

# Retrieve the token
$token  = [System.Environment]::GetEnvironmentVariable("INFLUXDB_ADMIN_TOKEN")
$bucket = [System.Environment]::GetEnvironmentVariable("INFLUXDB_BUCKET")
$org    = [System.Environment]::GetEnvironmentVariable("INFLUXDB_ORG")
$url    = "http://localhost:8086"

# Define query and headers
$query = @"
from(bucket:"$bucket")
  |> range(start: time(v: "2024-07-01T00:00:00Z"), stop: time(v: "2025-07-01T00:00:00Z"))
"@

$headers = @{
    "Authorization" = "Token $token"
    "Content-Type"  = "application/vnd.flux"
    "Accept"        = "application/csv"
}

# Send Flux script directly as plain text
Invoke-RestMethod -Uri "$url/api/v2/query?org=$org" `
    -Method POST `
    -Headers $headers `
    -Body $query `
    -OutFile "result.csv"