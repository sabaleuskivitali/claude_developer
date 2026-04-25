using System.Runtime.InteropServices;

namespace Seamlean.Agent.Management;

/// <summary>
/// Reads the username and domain of the user on the active physical console session
/// via the WTS (Windows Terminal Services) API.
///
/// Unlike Environment.UserName, this works correctly when called from a Windows Service
/// running as LocalSystem — it returns the human user at the keyboard, not "SYSTEM".
/// </summary>
internal static class WtsSessionHelper
{
    private enum WTS_INFO_CLASS
    {
        WTSUserName   = 5,
        WTSDomainName = 7,
    }

    [DllImport("kernel32.dll")]
    private static extern uint WTSGetActiveConsoleSessionId();

    [DllImport("wtsapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool WTSQuerySessionInformation(
        IntPtr         hServer,         // IntPtr.Zero = local server
        uint           sessionId,
        WTS_INFO_CLASS wtsInfoClass,
        out IntPtr     ppBuffer,
        out uint       pBytesReturned);

    [DllImport("wtsapi32.dll")]
    private static extern void WTSFreeMemory(IntPtr pMemory);

    private const uint NO_ACTIVE_SESSION = 0xFFFFFFFF;

    /// <summary>
    /// Returns (username, domain) of the active console user, or (null, null) if no
    /// user is logged in or the call fails (e.g. running in a headless environment).
    /// </summary>
    public static (string? Username, string? Domain) GetConsoleSessionUser()
    {
        try
        {
            var sessionId = WTSGetActiveConsoleSessionId();
            if (sessionId == NO_ACTIVE_SESSION)
                return (null, null);

            var username = QueryString(sessionId, WTS_INFO_CLASS.WTSUserName);
            var domain   = QueryString(sessionId, WTS_INFO_CLASS.WTSDomainName);

            // Empty string = session exists but no user is logged in (e.g. lock screen with no account)
            if (string.IsNullOrEmpty(username))
                return (null, null);

            return (username, domain);
        }
        catch
        {
            return (null, null);
        }
    }

    private static string? QueryString(uint sessionId, WTS_INFO_CLASS infoClass)
    {
        if (!WTSQuerySessionInformation(
                IntPtr.Zero, sessionId, infoClass,
                out var buffer, out _))
            return null;

        try
        {
            return Marshal.PtrToStringUni(buffer);
        }
        finally
        {
            WTSFreeMemory(buffer);
        }
    }
}
