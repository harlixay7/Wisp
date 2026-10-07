param([string]$Path, [int]$TimeoutSec = 120)
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
          $img.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
          $img.Dispose()
          $sha = [System.Security.Cryptography.SHA1]::Create()
          $hash = [System.BitConverter]::ToString($sha.ComputeHash($stream.ToArray()))
          $stream.Dispose()
          return $hash
        }
      }
    } catch { }
    return $null
  }

  function Save-ClipboardImage([string]$target) {
    if ([System.Windows.Forms.Clipboard]::ContainsImage()) {
      $image = [System.Windows.Forms.Clipboard]::GetImage()
      if ($image) {
        $dir = Split-Path -Parent $target
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
          New-Item -ItemType Directory -Force -Path $dir | Out-Null
        }
        $image.Save($target, [System.Drawing.Imaging.ImageFormat]::Png)
        $image.Dispose()
        return $true
      }
    }
    return $false
  }

  # Guards against stale clipboard images: only accept an image that differs
  # from whatever was already on the clipboard before the snip started.
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
  exit 2
}
