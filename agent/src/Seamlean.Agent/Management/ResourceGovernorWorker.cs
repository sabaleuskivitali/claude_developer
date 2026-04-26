using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Extensions.Options;

namespace Seamlean.Agent.Management;

/// <summary>
/// Samples agent CPU% and system RAM% every 10 seconds, feeds ResourceGovernor.
/// CPU% = delta(TotalProcessorTime) / (elapsed × processorCount) × 100.
/// RAM% = GlobalMemoryStatusEx dwMemoryLoad.
/// </summary>
public sealed class ResourceGovernorWorker : BackgroundService
{
    private readonly ResourceGovernor _governor;
    private readonly ILogger<ResourceGovernorWorker> _logger;

    private TimeSpan _prevCpuTime = TimeSpan.Zero;
    private DateTime _prevSampleAt = DateTime.UtcNow;

    public ResourceGovernorWorker(
        ResourceGovernor governor,
        ILogger<ResourceGovernorWorker> logger)
    {
        _governor = governor;
        _logger   = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        // Warm up the first CPU sample
        var proc = Process.GetCurrentProcess();
        proc.Refresh();
        _prevCpuTime  = proc.TotalProcessorTime;
        _prevSampleAt = DateTime.UtcNow;

        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(10));
        while (await timer.WaitForNextTickAsync(ct))
        {
            try
            {
                var (agentCpu, systemRam) = Sample();
                _governor.Update(agentCpu, systemRam);
            }
            catch (Exception ex)
            {
                _logger.LogWarning("ResourceGovernorWorker: {Msg}", ex.Message);
            }
        }
    }

    private (double agentCpuPct, double systemRamPct) Sample()
    {
        var proc = Process.GetCurrentProcess();
        proc.Refresh();

        var now        = DateTime.UtcNow;
        var cpuDelta   = proc.TotalProcessorTime - _prevCpuTime;
        var wallDelta  = (now - _prevSampleAt).TotalMilliseconds;

        _prevCpuTime  = proc.TotalProcessorTime;
        _prevSampleAt = now;

        double agentCpuPct = wallDelta > 0
            ? cpuDelta.TotalMilliseconds / (wallDelta * Environment.ProcessorCount) * 100.0
            : 0;

        double systemRamPct = ReadSystemRamUsedPct();

        return (agentCpuPct, systemRamPct);
    }

    private static double ReadSystemRamUsedPct()
    {
        var status = new MEMORYSTATUSEX { dwLength = (uint)Marshal.SizeOf<MEMORYSTATUSEX>() };
        return GlobalMemoryStatusEx(ref status) ? status.dwMemoryLoad : 0;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MEMORYSTATUSEX
    {
        public uint  dwLength;
        public uint  dwMemoryLoad;        // % of physical memory in use
        public ulong ullTotalPhys;
        public ulong ullAvailPhys;
        public ulong ullTotalPageFile;
        public ulong ullAvailPageFile;
        public ulong ullTotalVirtual;
        public ulong ullAvailVirtual;
        public ulong ullAvailExtendedVirtual;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GlobalMemoryStatusEx(ref MEMORYSTATUSEX lpBuffer);
}
