using System.Text.Json.Serialization;

namespace WinDiagSvc.Bootstrap;

public sealed record BootstrapEndpoints
{
    [JsonPropertyName("primary")]   public string  Primary   { get; init; } = "";
    [JsonPropertyName("secondary")] public string? Secondary { get; init; }
    [JsonPropertyName("policy")]    public string? Policy    { get; init; }
}

public sealed record BootstrapTrust
{
    [JsonPropertyName("ca_cert")] public string   CaCert { get; init; } = "";
    [JsonPropertyName("pins")]    public string[] Pins   { get; init; } = [];
}

public sealed record BootstrapEnrollment
{
    [JsonPropertyName("token")]        public string Token       { get; init; } = "";
    [JsonPropertyName("csr_endpoint")] public string CsrEndpoint { get; init; } = "";
    [JsonPropertyName("expires_at")]   public string ExpiresAt   { get; init; } = "";
}

public sealed record BootstrapProfile
{
    [JsonPropertyName("profile_id")] public string             ProfileId  { get; init; } = "";
    [JsonPropertyName("version")]    public string             Version    { get; init; } = "1";
    [JsonPropertyName("tenant_id")]  public string             TenantId   { get; init; } = "";
    [JsonPropertyName("site_id")]    public string             SiteId     { get; init; } = "";
    [JsonPropertyName("issued_at")]  public string             IssuedAt   { get; init; } = "";
    [JsonPropertyName("expires_at")] public string             ExpiresAt  { get; init; } = "";
    [JsonPropertyName("endpoints")]  public BootstrapEndpoints Endpoints  { get; init; } = new();
    [JsonPropertyName("trust")]      public BootstrapTrust     Trust      { get; init; } = new();
    [JsonPropertyName("enrollment")] public BootstrapEnrollment Enrollment { get; init; } = new();

    public bool IsExpired =>
        DateTime.TryParse(ExpiresAt, out var dt) && dt.ToUniversalTime() < DateTime.UtcNow;
}

/// <summary>
/// Wire format transmitted to agents.
/// signed_data = base64(canonical JSON of BootstrapProfile)
/// signature   = base64(ECDSA-P256-SHA256 DER signature of signed_data bytes)
/// </summary>
public sealed record SignedBootstrapProfile
{
    [JsonPropertyName("signed_data")] public string SignedData { get; init; } = "";
    [JsonPropertyName("signature")]   public string Signature  { get; init; } = "";

    public BootstrapProfile GetProfile()
    {
        var json = System.Text.Encoding.UTF8.GetString(Convert.FromBase64String(SignedData));
        return System.Text.Json.JsonSerializer.Deserialize<BootstrapProfile>(json)
               ?? throw new InvalidOperationException("Invalid bootstrap profile JSON.");
    }
}
