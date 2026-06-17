<#
.SYNOPSIS
    Idempotent update/removal of ~/.config/opencode/opencode.json (Windows).
.DESCRIPTION
    Without arguments, merges the "m365-copilot-proxy" provider into the config.
    With -Remove (or --remove), removes that provider.
.PARAMETER Remove
    Remove the provider instead of merging.
.EXAMPLE
    .\opencode.ps1
    .\opencode.ps1 -Remove
    .\opencode.ps1 --remove
#>

param(
    [switch]$Remove
)

# Also support the --remove style (common in bash)
if ($args -contains '--remove') {
    $Remove = $true
}

$CONFIG_FILE = Join-Path $HOME ".config\opencode\opencode.json"
$NEW_CONTENT = @'
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "m365-copilot-proxy": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "M365 Copilot Proxy",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "dummy"
      },
      "models": {
        "m365-auto": {},
        "m365-copilot": {},
        "m365-copilot:persist": {},
        "m365-opus": {},
        "m365-opus:persist": {},
        "m365-claude": {},
        "m365-claude:persist": {},
        "m365-gpt-quick": {},
        "m365-gpt-quick:persist": {},
        "m365-gpt-think": {},
        "m365-gpt-think:persist": {}
      }
    }
  }
}
'@

# Ensure the parent directory exists
$parent = Split-Path $CONFIG_FILE -Parent
if (-not (Test-Path $parent)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
}

# -------------------------------------------------------------------
# Deep merge of two PowerShell objects (dictionaries)
# -------------------------------------------------------------------
function Merge-JsonObjects($baseObj, $overrideObj) {
    if ($null -eq $overrideObj) { return $baseObj }
    if ($null -eq $baseObj)    { return $overrideObj }

    # Start with a shallow copy of the base
    $result = $baseObj.PSObject.Copy()

    foreach ($prop in $overrideObj.PSObject.Properties) {
        $propName = $prop.Name
        $propValue = $prop.Value

        $baseProp = $baseObj.PSObject.Properties[$propName]
        if ($baseProp -and $baseProp.Value -is [PSCustomObject] -and $propValue -is [PSCustomObject]) {
            # Recurse for nested objects
            $merged = Merge-JsonObjects $baseProp.Value $propValue
            $result.PSObject.Properties[$propName].Value = $merged
        } else {
            # Overwrite or add new property
            $result | Add-Member -MemberType NoteProperty -Name $propName -Value $propValue -Force
        }
    }
    return $result
}

# -------------------------------------------------------------------
# Merge the new provider into the existing config
# -------------------------------------------------------------------
function Do-Merge {
    $newJson = $NEW_CONTENT | ConvertFrom-Json

    if (-not (Test-Path $CONFIG_FILE)) {
        # Write the new config as-is
        $newJson | ConvertTo-Json -Depth 10 | Set-Content -Path $CONFIG_FILE -Encoding utf8
        Write-Host "Created new configuration file."
        return
    }

    # Read existing config
    $existing = Get-Content $CONFIG_FILE -Raw | ConvertFrom-Json

    # Merge provider: if existing has provider, merge deeply; otherwise just use new provider
    $mergedProvider = if ($existing.provider) {
        Merge-JsonObjects $existing.provider $newJson.provider
    } else {
        $newJson.provider
    }

    # Build the final object: start with existing, replace provider
    $merged = $existing.PSObject.Copy()
    $merged | Add-Member -MemberType NoteProperty -Name "provider" -Value $mergedProvider -Force

    # Ensure the $schema property is present (take it from new if missing)
    if ($newJson.PSObject.Properties['$schema'] -and -not $merged.PSObject.Properties['$schema']) {
        $merged | Add-Member -MemberType NoteProperty -Name '$schema' -Value $newJson.'$schema' -Force
    }

    # Compare old and new content
    $oldContent = Get-Content $CONFIG_FILE -Raw
    $newContent = $merged | ConvertTo-Json -Depth 10

    if ($oldContent -ne $newContent) {
        $newContent | Set-Content -Path $CONFIG_FILE -Encoding utf8
        Write-Host "Setup completed!"
    } else {
        Write-Host "Configuration already up-to-date."
    }
}

# -------------------------------------------------------------------
# Remove the provider
# -------------------------------------------------------------------
function Do-Remove {
    if (-not (Test-Path $CONFIG_FILE)) {
        Write-Host "Configuration file does not exist. Nothing removed."
        return
    }

    $existing = Get-Content $CONFIG_FILE -Raw | ConvertFrom-Json

    # Check if the provider exists
    if ($existing.provider -and $existing.provider.PSObject.Properties['m365-copilot-proxy']) {
        # Remove the specific provider
        $existing.provider.PSObject.Properties.Remove('m365-copilot-proxy')

        # If provider is now empty, remove it as well
        if ($existing.provider.PSObject.Properties.Count -eq 0) {
            $existing.PSObject.Properties.Remove('provider')
        }

        $newContent = $existing | ConvertTo-Json -Depth 10
        $oldContent = Get-Content $CONFIG_FILE -Raw

        if ($oldContent -ne $newContent) {
            $newContent | Set-Content -Path $CONFIG_FILE -Encoding utf8
            Write-Host "Removed 'm365-copilot-proxy' provider from configuration."
        } else {
            Write-Host "No changes. Provider was already absent."
        }
    } else {
        Write-Host "No changes. Provider was already absent."
    }
}

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
if ($Remove) {
    Do-Remove
} else {
    Do-Merge
}