export function toNetscape(cookies) {
  const lines = ["# Netscape HTTP Cookie File"];
  for (const cookie of cookies) {
    const domain = cookie.domain || "";
    const includeSubdomains = domain.startsWith(".") ? "TRUE" : "FALSE";
    const path = cookie.path || "/";
    const secure = cookie.secure ? "TRUE" : "FALSE";
    const expires = cookie.session || !cookie.expirationDate ? 0 : Math.floor(cookie.expirationDate);
    const prefix = cookie.httpOnly ? "#HttpOnly_" : "";
    lines.push(`${prefix}${domain}\t${includeSubdomains}\t${path}\t${secure}\t${expires}\t${cookie.name}\t${cookie.value}`);
  }
  return lines.join("\n") + "\n";
}