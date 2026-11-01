# Home Manager module: the unilib client (unilib-client, unilib-local-mcp), its config,
# the /ingest skill for Claude Code, and optional Claude Code MCP registration.
self:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.programs.unilib-client;
  inherit (lib) mkOption types;

  # Prints the Authorization header at connect time, so the token never enters
  # the Nix store or ~/.claude.json (Claude Code's MCP `headersHelper`).
  headersHelper = pkgs.writeShellScript "unilib-mcp-headers" ''
    token=$(${pkgs.coreutils}/bin/tr -d '[:space:]' < ${lib.escapeShellArg cfg.claudeCode.tokenFile})
    ${lib.getExe pkgs.jq} -nc --arg t "$token" '{Authorization: ("Bearer " + $t)}'
  '';

  servers = {
    unilib = {
      type = "http";
      url = cfg.claudeCode.serverUrl;
      headersHelper = "${headersHelper}";
    };
    unilib-local = {
      type = "stdio";
      command = "${cfg.package}/bin/unilib-local-mcp";
      args = [ ];
    };
  };
in
{
  options.programs.unilib-client = {
    enable = lib.mkEnableOption "the unilib client (unilib-client, unilib-local-mcp)";

    package = mkOption {
      type = types.package;
      default = pkgs.callPackage "${self}/nix/package.nix" { server = false; };
      defaultText = lib.literalExpression "unilib-client built from this pkgs";
      description = "The unilib client package.";
    };

    settings = mkOption {
      type = (pkgs.formats.toml { }).type;
      default = { };
      example = {
        papers.bib = "~/Zotero/exports/uni.bib";
        webdav = {
          url = "http://server:8091";
          user = "unilib";
          password_file = "/run/secrets/unilib-webdav";
        };
      };
      description = "Contents of ~/.config/unilib/client.toml ([papers], [webdav]); empty writes no file.";
    };

    ingestSkill.enable = lib.mkEnableOption "the /ingest skill at ~/.claude/skills/ingest";

    claudeCode = {
      register = lib.mkEnableOption ''
        registering the `unilib` (HTTP) and `unilib-local` (stdio) MCP servers in ~/.claude.json
        (user scope). Only those two entries are written; the rest of the file is left as is'';
      serverUrl = mkOption {
        type = types.str;
        example = "http://server:8095/mcp";
        description = "The unilib server's MCP endpoint.";
      };
      tokenFile = mkOption {
        type = types.str;
        description = "File holding the server's bearer token, read when Claude Code connects.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];

    xdg.configFile."unilib/client.toml" = lib.mkIf (cfg.settings != { }) {
      source = (pkgs.formats.toml { }).generate "unilib-client.toml" cfg.settings;
    };

    home.file.".claude/skills/ingest" = lib.mkIf cfg.ingestSkill.enable {
      source = "${cfg.package}/share/unilib/skills/ingest";
    };

    # ~/.claude.json is Claude Code's own mutable state, so it cannot be a store
    # symlink: merge just our two entries in, write only on change, and replace
    # the file atomically (temp file in the same directory, then mv).
    home.activation.unilibClaudeMcp = lib.mkIf cfg.claudeCode.register (
      lib.hm.dag.entryAfter [ "writeBoundary" ] ''
        file="$HOME/.claude.json"
        desired=${lib.escapeShellArg (builtins.toJSON servers)}
        if [ ! -e "$file" ]; then
          run ${lib.getExe pkgs.jq} -n --argjson s "$desired" '{mcpServers: $s}' > "$file"
        elif ${lib.getExe pkgs.jq} -e . "$file" >/dev/null 2>&1; then
          tmp="$(${pkgs.coreutils}/bin/mktemp "$file.unilib.XXXXXX")"
          ${lib.getExe pkgs.jq} --argjson s "$desired" '.mcpServers = ((.mcpServers // {}) + $s)' "$file" > "$tmp"
          if ${pkgs.diffutils}/bin/cmp -s "$tmp" "$file"; then
            ${pkgs.coreutils}/bin/rm -f "$tmp"
          else
            ${pkgs.coreutils}/bin/chmod --reference="$file" "$tmp"
            run ${pkgs.coreutils}/bin/mv -f "$tmp" "$file"
          fi
        else
          echo "unilibClaudeMcp: $file is not valid JSON; leaving it untouched" >&2
        fi
      ''
    );
  };
}
