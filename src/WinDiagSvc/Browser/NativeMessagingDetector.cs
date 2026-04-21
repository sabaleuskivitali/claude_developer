using System.Runtime.InteropServices;

namespace WinDiagSvc.Browser;

/// <summary>
/// Detects whether this process was launched by Chrome/Edge as a native messaging host
/// (stdin is a Windows pipe, not a console or null handle), then runs a minimal
/// stdin→queue-file→stdout loop without any DI or SQLite.
///
/// The main service reads browser_queue.jsonl via BrowserQueueImporter and writes
/// events to SQLite. This completely avoids SQLite contention between processes.
/// </summary>
public static class NativeMessagingDetector
{
    private const uint FILE_TYPE_PIPE = 3;
    private const int  STD_INPUT_HANDLE = -10;

    [DllImport("kernel32.dll")] private static extern nint GetStdHandle(int nStdHandle);
    [DllImport("kernel32.dll")] private static extern uint GetFileType(nint hFile);

    /// <summary>
    /// Returns true when stdin is a Windows named/anonymous pipe — i.e. Chrome launched us.
    /// Uses Win32 directly to avoid Console class issues in WinExe/WPF processes.
    /// </summary>
    public static bool IsNativeMessagingHost()
    {
        try
        {
            var handle = GetStdHandle(STD_INPUT_HANDLE);
            if (handle == nint.Zero || handle == new nint(-1)) return false;
            return GetFileType(handle) == FILE_TYPE_PIPE;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// Minimal native messaging loop.
    /// Reads length-prefixed JSON from stdin, appends to queue file, acks to stdout.
    /// No dependencies on DI / EventStore / SQLite.
    /// </summary>
    public static async Task RunAsync()
    {
        var queueFile = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            "Microsoft", "Diagnostics", "browser_queue.jsonl");

        // Ensure directory exists (main service creates it, but host might start first)
        try { Directory.CreateDirectory(Path.GetDirectoryName(queueFile)!); }
        catch { /* best effort */ }

        var inStream  = Console.OpenStandardInput();
        var outStream = Console.OpenStandardOutput();
        var lenBuf    = new byte[4];

        while (true)
        {
            // Read 4-byte LE length prefix
            var totalRead = 0;
            while (totalRead < 4)
            {
                var n = await inStream.ReadAsync(lenBuf.AsMemory(totalRead, 4 - totalRead));
                if (n == 0) return; // stdin closed — extension disconnected
                totalRead += n;
            }

            var msgLen = BitConverter.ToInt32(lenBuf, 0);
            if (msgLen <= 0 || msgLen > 1_048_576)
            {
                // Invalid length — send empty ack and continue
                await SendAckAsync(outStream);
                continue;
            }

            var msgBuf   = new byte[msgLen];
            var bodyRead = 0;
            while (bodyRead < msgLen)
            {
                var n = await inStream.ReadAsync(msgBuf.AsMemory(bodyRead, msgLen - bodyRead));
                if (n == 0) return;
                bodyRead += n;
            }

            // Append JSON line to queue file (main service imports asynchronously)
            try
            {
                var line = System.Text.Encoding.UTF8.GetString(msgBuf) + "\n";
                await File.AppendAllTextAsync(queueFile, line);
            }
            catch { /* ignore — do not crash the host on file errors */ }

            await SendAckAsync(outStream);
        }
    }

    private static async Task SendAckAsync(Stream outStream)
    {
        var ack    = "{\"ok\":true}"u8.ToArray();
        var ackLen = BitConverter.GetBytes(ack.Length);
        await outStream.WriteAsync(ackLen);
        await outStream.WriteAsync(ack);
        await outStream.FlushAsync();
    }
}
