using System.Numerics;
using System.Runtime.InteropServices;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using SkiaSharp;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;

namespace WinDiagSvc.Capture;

public sealed class ScreenshotWorker : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger<ScreenshotWorker> _logger;

    private ulong _lastDHash;

    public ScreenshotWorker(
        EventStore store,
        NtpSynchronizer ntp,
        IOptions<AgentSettings> options,
        ILogger<ScreenshotWorker> logger)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
        _logger   = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        var dir = _settings.ExpandedScreenshotDir;
        Directory.CreateDirectory(dir);

        var interval = TimeSpan.FromSeconds(_settings.ScreenshotIntervalSeconds);
        using var timer = new PeriodicTimer(interval);

        while (await timer.WaitForNextTickAsync(ct))
        {
            try { CaptureAndStore("periodic_10s"); }
            catch (Exception ex) { WriteLayerError(ex); }
        }
    }

    /// <summary>
    /// Called by WindowWatcher / UiAutomationCapture when a significant event occurs.
    /// </summary>
    public void TriggerCapture(string reason)
    {
        try { CaptureAndStore(reason); }
        catch (Exception ex) { WriteLayerError(ex); }
    }

    private void CaptureAndStore(string reason)
    {
        using var bitmap = CaptureActiveWindow();
        if (bitmap is null) return;

        var hash = ComputeDHash(bitmap);

        // Skip duplicate periodic screenshots — always save on triggered events
        if (reason == "periodic_10s" &&
            HammingDistance(_lastDHash, hash) < _settings.DHashDistanceThreshold)
            return;

        _lastDHash = hash;

        var date    = DateTime.UtcNow.ToString("yyyyMMdd");
        var dir     = Path.Combine(_settings.ExpandedScreenshotDir, date);
        Directory.CreateDirectory(dir);

        var eventId = Guid.NewGuid();
        var path    = Path.Combine(dir, $"{eventId}.webp");

        using var image = SKImage.FromBitmap(bitmap);
        using var data  = image.Encode(SKEncodedImageFormat.Webp, 80);
        using var fs    = File.OpenWrite(path);
        data.SaveTo(fs);

        // Store relative path for portability
        var relativePath = Path.Combine(date, $"{eventId}.webp");

        var raw = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        _store.Insert(new ActivityEvent
        {
            EventId       = eventId,
            SessionId     = _store.SessionId,
            MachineId     = _settings.MachineId,
            UserId        = _settings.UserId,
            TimestampUtc  = raw,
            SyncedTs      = _ntp.SyncedTs(raw),
            DriftMs       = _ntp.CurrentDriftMs,
            DriftRatePpm  = _ntp.DriftRatePpm,
            Layer         = "visual",
            EventType     = nameof(EventType.Screenshot),
            ScreenshotPath  = relativePath,
            ScreenshotDHash = hash,
            CaptureReason   = reason,
        });
    }

    private static SKBitmap? CaptureActiveWindow()
    {
        var hwnd = GetForegroundWindow();
        if (hwnd == nint.Zero) return null;

        if (!GetWindowRect(hwnd, out var rect)) return null;

        var w = rect.Right  - rect.Left;
        var h = rect.Bottom - rect.Top;
        if (w <= 0 || h <= 0) return null;

        var hdc    = GetDC(hwnd);
        var memDc  = CreateCompatibleDC(hdc);
        var hBmp   = CreateCompatibleBitmap(hdc, w, h);
        var oldBmp = SelectObject(memDc, hBmp);

        try
        {
            BitBlt(memDc, 0, 0, w, h, hdc, 0, 0, SRCCOPY);

            var bmpInfo = new BITMAPINFOHEADER
            {
                biSize        = (uint)Marshal.SizeOf<BITMAPINFOHEADER>(),
                biWidth       = w,
                biHeight      = -h,   // top-down
                biPlanes      = 1,
                biBitCount    = 32,
                biCompression = BI_RGB,
            };

            var pixels = new byte[w * h * 4];
            GetDIBits(memDc, hBmp, 0, (uint)h, pixels, ref bmpInfo, DIB_RGB_COLORS);

            var bmp = new SKBitmap(w, h, SKColorType.Bgra8888, SKAlphaType.Opaque);
            unsafe
            {
                fixed (byte* ptr = pixels)
                    Buffer.MemoryCopy(ptr, bmp.GetPixels().ToPointer(), pixels.Length, pixels.Length);
            }
            return bmp;
        }
        finally
        {
            SelectObject(memDc, oldBmp);
            DeleteObject(hBmp);
            DeleteDC(memDc);
            ReleaseDC(hwnd, hdc);
        }
    }

    private static ulong ComputeDHash(SKBitmap bmp)
    {
        using var small = bmp.Resize(new SKImageInfo(9, 8), SKFilterQuality.Low);
        ulong hash = 0;
        for (var y = 0; y < 8; y++)
            for (var x = 0; x < 8; x++)
                if (small.GetPixel(x, y).Red > small.GetPixel(x + 1, y).Red)
                    hash |= 1UL << (y * 8 + x);
        return hash;
    }

    private static int HammingDistance(ulong a, ulong b) =>
        BitOperations.PopCount(a ^ b);

    private void WriteLayerError(Exception ex) =>
        _store.Insert(new ActivityEvent
        {
            SessionId    = _store.SessionId,
            MachineId    = _settings.MachineId,
            UserId       = _settings.UserId,
            TimestampUtc = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            SyncedTs     = _ntp.SyncedTs(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()),
            DriftMs      = _ntp.CurrentDriftMs,
            DriftRatePpm = _ntp.DriftRatePpm,
            Layer        = "visual",
            EventType    = nameof(EventType.LayerError),
            RawMessage   = ex.Message[..Math.Min(ex.Message.Length, 500)],
        });

    // -----------------------------------------------------------------------
    // P/Invoke
    // -----------------------------------------------------------------------

    private const uint SRCCOPY      = 0x00CC0020;
    private const uint BI_RGB       = 0;
    private const uint DIB_RGB_COLORS = 0;

    [DllImport("user32.dll")] private static extern nint GetForegroundWindow();
    [DllImport("user32.dll")] private static extern bool GetWindowRect(nint hWnd, out RECT lpRect);
    [DllImport("user32.dll")] private static extern nint GetDC(nint hWnd);
    [DllImport("user32.dll")] private static extern int ReleaseDC(nint hWnd, nint hDC);
    [DllImport("gdi32.dll")]  private static extern nint CreateCompatibleDC(nint hDC);
    [DllImport("gdi32.dll")]  private static extern nint CreateCompatibleBitmap(nint hDC, int nWidth, int nHeight);
    [DllImport("gdi32.dll")]  private static extern nint SelectObject(nint hDC, nint hObject);
    [DllImport("gdi32.dll")]  private static extern bool DeleteObject(nint hObject);
    [DllImport("gdi32.dll")]  private static extern bool DeleteDC(nint hDC);
    [DllImport("gdi32.dll")]  private static extern bool BitBlt(nint hDC, int nXDest, int nYDest, int nWidth, int nHeight, nint hSrcDC, int nXSrc, int nYSrc, uint dwRop);
    [DllImport("gdi32.dll")]  private static extern int GetDIBits(nint hDC, nint hBitmap, uint uStartScan, uint cScanLines, byte[] lpvBits, ref BITMAPINFOHEADER lpbi, uint uUsage);

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    private struct BITMAPINFOHEADER
    {
        public uint biSize, biWidth; public int biHeight;
        public ushort biPlanes, biBitCount;
        public uint biCompression, biSizeImage, biXPelsPerMeter, biYPelsPerMeter, biClrUsed, biClrImportant;
    }
}
