Write-Host "=== PersonalGameAI system check v0.4.0 ===" -ForegroundColor Cyan
Write-Host ""

Write-Host "Windows:" -ForegroundColor Yellow
Get-CimInstance Win32_OperatingSystem |
    Select-Object Caption, Version, OSArchitecture |
    Format-List

Write-Host "CPU:" -ForegroundColor Yellow
Get-CimInstance Win32_Processor |
    Select-Object Name, NumberOfCores, NumberOfLogicalProcessors |
    Format-List

Write-Host "RAM visible to Windows:" -ForegroundColor Yellow
$ram = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB
"{0:N1} GB" -f $ram

Write-Host ""
Write-Host "Physical memory modules:" -ForegroundColor Yellow
Get-CimInstance Win32_PhysicalMemory |
    Select-Object BankLabel, DeviceLocator,
        @{Name="CapacityGB";Expression={[math]::Round($_.Capacity / 1GB, 1)}},
        Speed, Manufacturer, PartNumber |
    Format-Table -AutoSize

Write-Host ""
Write-Host "GPU:" -ForegroundColor Yellow
Get-CimInstance Win32_VideoController |
    Select-Object Name, DriverVersion |
    Format-List

Write-Host "NVIDIA check:" -ForegroundColor Yellow
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    nvidia-smi
} else {
    Write-Host "nvidia-smi not found (normal for AMD/Intel GPUs)."
}

Write-Host ""
Write-Host "Python launchers:" -ForegroundColor Yellow
if (Get-Command py -ErrorAction SilentlyContinue) {
    py -0p
    Write-Host ""
    Write-Host "Default Python:" -ForegroundColor Yellow
    py --version
} else {
    Write-Host "Python launcher not found."
}

Write-Host ""
Write-Host "Git:" -ForegroundColor Yellow
if (Get-Command git -ErrorAction SilentlyContinue) {
    git --version
} else {
    Write-Host "Git not found."
}
