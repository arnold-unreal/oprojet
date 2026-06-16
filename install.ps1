# Arrêter et supprimer si déjà installé
Stop-Service AgentMonitor -Force -ErrorAction SilentlyContinue
sc.exe delete AgentMonitor | Out-Null
Start-Sleep -Seconds 2
New-Item -ItemType Directory -Path 'C:\ProgramData\AgentMonitor\logs' -Force | Out-Null
Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/arnold-unreal/oprojet/main/agent_v2.py' -OutFile 'C:\ProgramData\AgentMonitor\agent_v2.py'
Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/arnold-unreal/oprojet/main/service_wrapper.py' -OutFile 'C:\ProgramData\AgentMonitor\service_wrapper.py'
& 'C:\Program Files\Python312\python.exe' 'C:\ProgramData\AgentMonitor\service_wrapper.py' install | Out-Null
Set-Service AgentMonitor -StartupType Automatic
Start-Service AgentMonitor
