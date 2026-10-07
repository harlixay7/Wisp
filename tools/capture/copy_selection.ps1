param([int]$TimeoutMs = 750)
# Selection capture: simulate Ctrl+C, read the copied text, restore the
# previous TEXT clipboard content. Non-text clipboard content (images, files)
# is replaced by the target app's copy and cannot be restored here.
$ErrorActionPreference = "Stop"
try {
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
  $prev = $null
  if ([System.Windows.Forms.Clipboard]::ContainsText()) {
    $prev = [System.Windows.Forms.Clipboard]::GetText()
  }
  # Wait for hotkey modifiers to be released; ^c while Ctrl+Alt is held is
  # delivered as Ctrl+Alt+C and never copies.
  $modDeadline = [DateTime]::UtcNow.AddMilliseconds(900)
  while ([DateTime]::UtcNow -lt $modDeadline) {
    if ([System.Windows.Forms.Control]::ModifierKeys -eq [System.Windows.Forms.Keys]::None) { break }
    Start-Sleep -Milliseconds 20
  }
  [System.Windows.Forms.SendKeys]::SendWait('^c')
  $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMs)
  $found = $null
  while ([DateTime]::UtcNow -lt $deadline) {
    if ([System.Windows.Forms.Clipboard]::ContainsText()) {
      $current = [System.Windows.Forms.Clipboard]::GetText()
      if ($current -and $current -ne $prev) { $found = $current; break }
    }
    Start-Sleep -Milliseconds 40
  }
  if ($prev -ne $null) { [System.Windows.Forms.Clipboard]::SetText($prev) }
  if ($found) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($found)
    $stdout = [System.Console]::OpenStandardOutput()
    $stdout.Write($bytes, 0, $bytes.Length)
    $stdout.Flush()
  }
  exit 0
} catch {
  exit 2
}
