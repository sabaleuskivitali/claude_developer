namespace Seamlean.Agent.Capture.Meeting;

public sealed class MeetingRecord
{
    public string  MeetingId    { get; set; } = "";
    public string  MachineId    { get; set; } = "";
    public string  UserId       { get; set; } = "";
    public long    StartedAt    { get; set; }
    public long?   EndedAt      { get; set; }
    public string? ProcessName  { get; set; }
    public string? WindowTitle  { get; set; }
    public string? Trigger      { get; set; }
    public string? MicPath      { get; set; }
    public string? LoopbackPath { get; set; }
    public int     MicSent      { get; set; }
    public int     LoopbackSent { get; set; }
    public int     MetaSent     { get; set; }
}
