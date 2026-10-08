param([int]$TimeoutMs = 750)
# Selection capture: simulate Ctrl+C, write the copied text to stdout as UTF-8,
# then restore the previous text clipboard content. Non-text clipboard content
# (images, files) is replaced by the target app's copy and cannot be restored.
# Exit codes: 0 = done (empty stdout means nothing was selected), 2 = error,
# with the reason on stderr.
$ErrorActionPreference = "Stop"
try {
  Add-Type -AssemblyName System.Windows.Forms
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
  if ($null -ne $prev) { [System.Windows.Forms.Clipboard]::SetText($prev) }
  if ($found) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($found)
    $stdout = [System.Console]::OpenStandardOutput()
    $stdout.Write($bytes, 0, $bytes.Length)
    $stdout.Flush()
  }
  exit 0
} catch {
  [System.Console]::Error.WriteLine($_.Exception.Message)
  exit 2
}
