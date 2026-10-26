$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (!(Test-Path '.venv/Scripts/python.exe')) {
  py -3 -m venv .venv
  if ($LASTEXITCODE -ne 0) { throw 'Could not create virtual environment. Install Python 3.11+ with the Python launcher, then retry.' }
}
& .venv/Scripts/python.exe bootstrap.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($args.Count -eq 0) { & .venv/Scripts/python.exe -m work_copilot demo }
else { & .venv/Scripts/python.exe -m work_copilot @args }
exit $LASTEXITCODE
