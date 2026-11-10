// A C# WinForms application with the whole TurboSSH GUI inside one of its panels.
//
// Build with the C# compiler that ships with Windows (no SDK needed):
//   %WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /target:winexe
//       /r:System.Web.Extensions.dll /out:embed_host_winforms.exe embed_host_winforms.cs
// Run:
//   embed_host_winforms.exe "C:\path\to\turbossh-gui.exe"
//   embed_host_winforms.exe python -m turbossh.gui          (needs PyQt5)
//
// The TurboSSH class is all a .NET host needs: it starts TurboSSH with
// --embed <panel handle> --control stdio and speaks the JSON-lines protocol of
// EMBEDDING.md. WPF: give TurboSSH the handle of an HwndHost child instead.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

/// <summary>A running TurboSSH, living in a window of ours.</summary>
public sealed class TurboSSH : IDisposable
{
    readonly Process proc;
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly Dictionary<int, Reply> pending = new Dictionary<int, Reply>();
    readonly object gate = new object();
    readonly object writeGate = new object();
    int nextId;

    /// <summary>Set when TurboSSH has said "ready".</summary>
    public readonly ManualResetEvent Ready = new ManualResetEvent(false);

    /// <summary>Every event (session_opened, session_connected, log, ...). Raised
    /// on a background thread: BeginInvoke to touch the UI.</summary>
    public event Action<Dictionary<string, object>> EventReceived;

    sealed class Reply
    {
        public readonly ManualResetEvent Done = new ManualResetEvent(false);
        public Dictionary<string, object> Message;
    }

    public TurboSSH(string fileName, string arguments, IntPtr parent)
    {
        var info = new ProcessStartInfo(fileName,
            (arguments + " --embed " + parent.ToInt64() + " --control stdio").Trim());
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardInput = true;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        info.StandardOutputEncoding = Encoding.UTF8;
        info.StandardErrorEncoding = Encoding.UTF8;
        proc = Process.Start(info);
        proc.ErrorDataReceived += delegate { };     // keep stderr drained
        proc.BeginErrorReadLine();
        var reader = new Thread(ReadLoop);
        reader.IsBackground = true;
        reader.Start();
    }

    void ReadLoop()
    {
        string line;
        while ((line = proc.StandardOutput.ReadLine()) != null)
        {
            Dictionary<string, object> message;
            try { message = json.Deserialize<Dictionary<string, object>>(line); }
            catch (ArgumentException) { continue; }
            if (message.ContainsKey("event"))
            {
                if ((string)message["event"] == "ready") Ready.Set();
                var handler = EventReceived;
                if (handler != null) handler(message);
                continue;
            }
            Reply reply = null;
            lock (gate)
            {
                int id = Convert.ToInt32(message["id"]);
                if (pending.TryGetValue(id, out reply)) pending.Remove(id);
            }
            if (reply != null) { reply.Message = message; reply.Done.Set(); }
        }
        lock (gate)                                  // it exited: nobody will answer
        {
            foreach (var r in pending.Values) r.Done.Set();
            pending.Clear();
        }
    }

    /// <summary>One request; returns its result. TurboSSH answers at once, so this
    /// is fine on the UI thread (WaitOne keeps sent messages flowing there).</summary>
    public object Call(string cmd, Dictionary<string, object> args)
    {
        var reply = new Reply();
        int id;
        lock (gate) { id = ++nextId; pending[id] = reply; }
        var request = new Dictionary<string, object>();
        request["id"] = id;
        request["cmd"] = cmd;
        request["args"] = args ?? new Dictionary<string, object>();
        byte[] data = Encoding.UTF8.GetBytes(json.Serialize(request) + "\n");
        lock (writeGate)
        {
            proc.StandardInput.BaseStream.Write(data, 0, data.Length);
            proc.StandardInput.BaseStream.Flush();
        }
        if (!reply.Done.WaitOne(30000)) throw new TimeoutException(cmd + ": no answer");
        if (reply.Message == null) throw new InvalidOperationException("TurboSSH has exited");
        if (!(bool)reply.Message["ok"])
            throw new InvalidOperationException((string)reply.Message["error"]);
        return reply.Message["result"];
    }

    public static Dictionary<string, object> Args(params object[] pairs)
    {
        var d = new Dictionary<string, object>();
        for (int i = 0; i + 1 < pairs.Length; i += 2) d[(string)pairs[i]] = pairs[i + 1];
        return d;
    }

    public bool HasExited { get { return proc.HasExited; } }

    /// <summary>Close every session (remote ports are released) and end the process.</summary>
    public void Dispose()
    {
        try { if (!proc.HasExited) Call("shutdown", Args("wait", 6)); }
        catch (Exception) { }
        if (!proc.WaitForExit(20000)) proc.Kill();
    }
}

public sealed class HostForm : Form
{
    readonly Panel panel = new Panel();
    readonly Label status = new Label();
    readonly string fileName, arguments;
    public TurboSSH Turbo;

    public HostForm(string fileName, string arguments)
    {
        this.fileName = fileName;
        this.arguments = arguments;
        Text = "A WinForms application with TurboSSH inside";
        Size = new Size(1150, 720);
        var bar = new FlowLayoutPanel();
        bar.Dock = DockStyle.Top;
        bar.Height = 34;
        var host = new TextBox();
        host.Width = 160;
        host.Text = "10.0.0.5";
        var user = new TextBox();
        user.Width = 90;
        user.Text = "root";
        var connect = new Button();
        connect.Text = "Connect";
        connect.Click += delegate
        {
            Turbo.Call("connect_ssh", TurboSSH.Args("host", host.Text, "username", user.Text));
        };
        var local = new Button();
        local.Text = "Local terminal";
        local.AutoSize = true;
        local.Click += delegate { Turbo.Call("open_local_terminal", null); };
        status.AutoSize = true;
        status.Padding = new Padding(8, 6, 0, 0);
        bar.Controls.AddRange(new Control[] { host, user, connect, local, status });
        panel.Dock = DockStyle.Fill;                 // TurboSSH fills this panel
        Controls.Add(panel);
        Controls.Add(bar);
        Load += delegate
        {
            Turbo = new TurboSSH(this.fileName, this.arguments, panel.Handle);
            Turbo.EventReceived += OnEvent;
        };
        FormClosing += delegate { if (Turbo != null) Turbo.Dispose(); };
    }

    void OnEvent(Dictionary<string, object> e)
    {
        var name = (string)e["event"];
        if (name == "log" || !IsHandleCreated) return;
        try
        {
            BeginInvoke((MethodInvoker)delegate
            {
                status.Text = name + (e.ContainsKey("session") ? " " + e["session"] : "");
            });
        }
        catch (InvalidOperationException) { }       // the form is closing
    }
}

public static class Program
{
    [STAThread]
    public static void Main(string[] args)
    {
        string fileName = args.Length > 0 ? args[0] : "turbossh-gui.exe";
        string rest = args.Length > 1 ? string.Join(" ", args, 1, args.Length - 1) : "";
        Application.EnableVisualStyles();
        Application.Run(new HostForm(fileName, rest));
    }
}
