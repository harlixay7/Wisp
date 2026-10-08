param(
  [Parameter(Mandatory = $true)][string]$Path,
  [int]$TimeoutSec = 120
)
# Waits for a new image on the clipboard (the result of a screen snip) and
# saves it to $Path as PNG. An image already on the clipboard when the script
# starts is ignored, so a stale copy is never mistaken for the snip.
# Exit codes: 0 = saved, 1 = no new image before the timeout, 2 = error, with
# the reason on stderr.
$ErrorActionPreference = "Stop"
try {
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing

  function Get-ClipboardImageHash {
    try {
      if ([System.Windows.Forms.Clipboard]::ContainsImage()) {
        $img = [System.Windows.Forms.Clipboard]::GetImage()
        if ($img) {
          $stream = New-Object System.IO.MemoryStream
          $sha = [System.Security.Cryptography.SHA1]::Create()
          try {
            $img.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
            return [System.BitConverter]::ToString($sha.ComputeHash($stream.ToArray()))
          } finally {
            $img.Dispose()
            $sha.Dispose()
            $stream.Dispose()
          }
        }
      }
    } catch { }
    return $null
  }

  function Save-ClipboardImage([string]$target) {
    if ([System.Windows.Forms.Clipboard]::ContainsImage()) {
      $image = [System.Windows.Forms.Clipboard]::GetImage()
      if ($image) {
        try {
          $dir = Split-Path -Parent $target
          if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
          }
          $image.Save($target, [System.Drawing.Imaging.ImageFormat]::Png)
          return $true
        } finally {
          $image.Dispose()
        }
      }
    }
    return $false
  }

  $previousHash = Get-ClipboardImageHash
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    $currentHash = Get-ClipboardImageHash
    if ($currentHash -and $currentHash -ne $previousHash) {
      if (Save-ClipboardImage $Path) { exit 0 }
    }
    Start-Sleep -Milliseconds 150
  }
  exit 1
} catch {
  [System.Console]::Error.WriteLine($_.Exception.Message)
  exit 2
}
