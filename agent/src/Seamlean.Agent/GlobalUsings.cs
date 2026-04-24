// Global usings for the entire project.
// System.IO is NOT included in Microsoft.NET.Sdk.Worker implicit usings,
// so we add it here along with the ActivityEvent alias to avoid ambiguity
// with System.Diagnostics.ActivityEvent (.NET 8 distributed-tracing type).

global using System.IO;
global using ActivityEvent = Seamlean.Agent.Models.ActivityEvent;
