using System.ComponentModel;
using System.Diagnostics;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Runtime.InteropServices;

internal static class Program
{
    private const string Title = "Foundry quota";
    private const string Url = "http://127.0.0.1:8765/";
    private static readonly HttpClient Client = new(new HttpClientHandler
    {
        UseProxy = false,
        AllowAutoRedirect = false,
    })
    {
        Timeout = TimeSpan.FromSeconds(3),
        MaxResponseContentBufferSize = 2 * 1024 * 1024,
    };

    private enum PortState { Available, Dashboard, OtherService }

    [DllImport("user32.dll", EntryPoint = "MessageBoxW", CharSet = CharSet.Unicode)]
    private static extern int MessageBox(nint window, string text, string caption, uint type);

    private static async Task<int> Main()
    {
        try
        {
            string root = CheckoutRoot();
            PortState state = await ProbeAsync();
            if (state == PortState.OtherService)
            {
                throw new InvalidOperationException(
                    "Port 8765 is already in use by another service. The inventory was not opened.");
            }

            if (state == PortState.Available)
            {
                string powerShell = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.System),
                    "WindowsPowerShell", "v1.0", "powershell.exe");
                if (!File.Exists(powerShell))
                {
                    throw new FileNotFoundException("Windows PowerShell is required to start the dashboard.");
                }

                var start = new ProcessStartInfo(powerShell)
                {
                    UseShellExecute = true,
                    WorkingDirectory = root,
                    WindowStyle = ProcessWindowStyle.Normal,
                };
                start.ArgumentList.Add("-NoLogo");
                start.ArgumentList.Add("-NoProfile");
                start.ArgumentList.Add("-NoExit");
                start.ArgumentList.Add("-File");
                start.ArgumentList.Add(Path.Combine(root, "start-dashboard.ps1"));
                start.ArgumentList.Add("-DataDirectory");
                start.ArgumentList.Add(Path.Combine(root, "data"));

                using Process server = Process.Start(start)
                    ?? throw new InvalidOperationException("Windows PowerShell could not be started.");
                var deadline = Stopwatch.StartNew();
                while (deadline.Elapsed < TimeSpan.FromSeconds(25))
                {
                    await Task.Delay(250);
                    state = await ProbeAsync();
                    if (state == PortState.Dashboard)
                    {
                        break;
                    }
                    if (state == PortState.OtherService)
                    {
                        throw new InvalidOperationException(
                            "Another service answered on port 8765. The inventory was not opened.");
                    }
                    if (server.HasExited)
                    {
                        throw new InvalidOperationException(
                            "The dashboard process exited. Check the PowerShell window for details.");
                    }
                }
                if (state != PortState.Dashboard)
                {
                    throw new InvalidOperationException(
                        "The dashboard did not become ready. Check the PowerShell window for details.");
                }
            }

            try
            {
                _ = Process.Start(new ProcessStartInfo(Url) { UseShellExecute = true });
            }
            catch (Win32Exception error)
            {
                throw new InvalidOperationException(
                    $"The dashboard is running, but the default browser could not be opened. Visit {Url} manually.",
                    error);
            }
            return 0;
        }
        catch (Exception error) when (error is IOException or InvalidOperationException
            or Win32Exception or HttpRequestException or UnauthorizedAccessException)
        {
            MessageBox(0, error.Message, Title, 0x10);
            return 1;
        }
    }

    private static string CheckoutRoot()
    {
        var executableDirectory = new DirectoryInfo(AppContext.BaseDirectory);
        bool installedInData = executableDirectory.Name.Equals("launcher", StringComparison.OrdinalIgnoreCase)
            && string.Equals(executableDirectory.Parent?.Name, "data", StringComparison.OrdinalIgnoreCase);
        DirectoryInfo? root = installedInData ? executableDirectory.Parent?.Parent : executableDirectory;
        if (root is null
            || !File.Exists(Path.Combine(root.FullName, "start-dashboard.ps1"))
            || !File.Exists(Path.Combine(root.FullName, "dashboard", "__main__.py")))
        {
            throw new FileNotFoundException(
                "Place this EXE beside start-dashboard.ps1 or in the checkout's data\\launcher folder.");
        }
        return root.FullName;
    }

    private static async Task<PortState> ProbeAsync()
    {
        try
        {
            using HttpResponseMessage response = await Client.GetAsync(Url);
            if (response.StatusCode != HttpStatusCode.OK
                || response.Headers.Server.ToString() != "FoundryInventory")
            {
                return PortState.OtherService;
            }
            string page = await response.Content.ReadAsStringAsync();
            return page.Contains("Foundry inventory", StringComparison.OrdinalIgnoreCase)
                ? PortState.Dashboard : PortState.OtherService;
        }
        catch (HttpRequestException error) when (error.InnerException is SocketException socket
            && socket.SocketErrorCode == SocketError.ConnectionRefused)
        {
            return PortState.Available;
        }
        catch (TaskCanceledException)
        {
            return PortState.OtherService;
        }
    }
}
