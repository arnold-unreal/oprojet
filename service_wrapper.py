import sys,subprocess,win32serviceutil,win32service,win32event,servicemanager

class S(win32serviceutil.ServiceFramework):
    _svc_name_="AgentMonitor"
    _svc_display_name_="AgentMonitor"
    def __init__(self,a):
        win32serviceutil.ServiceFramework.__init__(self,a)
        self.e=win32event.CreateEvent(None,0,0,None)
        self.p=None
    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self.p: self.p.terminate()
        win32event.SetEvent(self.e)
    def SvcDoRun(self):
        self.p=subprocess.Popen(
            [r"C:\Program Files\Python312\python.exe",r"C:\ProgramData\AgentMonitor\agent.py"],
            cwd=r"C:\ProgramData\AgentMonitor",
            stdout=open(r"C:\ProgramData\AgentMonitor\logs\out.log","a"),
            stderr=open(r"C:\ProgramData\AgentMonitor\logs\err.log","a")
        )
        while self.p.poll() is None:
            if win32event.WaitForSingleObject(self.e,1000)==win32event.WAIT_OBJECT_0:
                break

if __name__=="__main__":
    win32serviceutil.HandleCommandLine(S)