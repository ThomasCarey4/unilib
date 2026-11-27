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
  }
  // lib.mapAttrs (_: l: http l.url) (lib.filterAttrs (_: l: l.projects == [ ]) cfg.claudeCode.extraLibraries);

  http = url: {
    type = "http";
    inherit url;
    headersHelper = "${headersHelper}";
  };

  # Local-scope entries (~/.claude.json projects.<path>.mcpServers): only sessions in those projects see them.
  projectServers = lib.foldlAttrs (
    acc: name: l:
    lib.foldl' (a: p: a // { ${p} = (a.${p} or { }) // { ${name} = http l.url; }; }) acc l.projects
  ) { } cfg.claudeCode.extraLibraries;

  desired = {
    user = servers;
    projects = projectServers;
  };
  # Merge our entries into Claude Code's state, leaving everything else as it is.
  jqMerge = ''
    .mcpServers = ((.mcpServers // {}) + $s.user)
    | reduce ($s.projects | to_entries[]) as $e (.;
        .projects[$e.key].mcpServers = ((.projects[$e.key].mcpServers // {}) + $e.value))
  '';
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

    lessonSkill.enable = lib.mkEnableOption "the /lesson skill (project lessons notes) at ~/.claude/skills/lesson";

    claudeCode = {
      register = lib.mkEnableOption ''
        registering the `unilib` (HTTP) and `unilib-local` (stdio) MCP servers, and any
        extraLibraries, in ~/.claude.json. Only those entries are written; the rest of the file is left as is'';
      serverUrl = mkOption {
        type = types.str;
        example = "http://server:8095/mcp";
        description = "The unilib server's MCP endpoint.";
      };
      tokenFile = mkOption {
        type = types.str;
        description = "File holding the server's bearer token, read when Claude Code connects.";
      };
      extraLibraries = mkOption {
        type = types.attrsOf (
          types.submodule {
            options = {
              url = mkOption {
                type = types.str;
                example = "http://server:8095/eng/mcp";
                description = "The library's MCP endpoint (same server and token as serverUrl).";
              };
              projects = mkOption {
                type = types.listOf types.str;
                default = [ ];
                example = [ "/home/alice/src/myproject" ];
                description = ''
                  Project directories whose Claude Code sessions get this server (local scope); empty
                  registers it for every session (user scope).
                '';
              };
            };
          }
        );
        default = { };
        example = lib.literalExpression ''{ unilib-eng = { url = "http://server:8095/eng/mcp"; projects = [ "/home/alice/src/app" ]; }; }'';
        description = "Further libraries of the same server to register, by MCP server name.";
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

    home.file.".claude/skills/lesson" = lib.mkIf cfg.lessonSkill.enable {
      source = "${cfg.package}/share/unilib/skills/lesson";
    };

    # ~/.claude.json is Claude Code's own mutable state, so it cannot be a store
    # symlink: merge just our two entries in, write only on change, and replace
    # the file atomically (temp file in the same directory, then mv).
    home.activation.unilibClaudeMcp = lib.mkIf cfg.claudeCode.register (
      lib.hm.dag.entryAfter [ "writeBoundary" ] ''
        file="$HOME/.claude.json"
        desired=${lib.escapeShellArg (builtins.toJSON desired)}
        if [ ! -e "$file" ]; then
          run ${lib.getExe pkgs.jq} -n --argjson s "$desired" ${lib.escapeShellArg "{} | ${jqMerge}"} > "$file"
        elif ${lib.getExe pkgs.jq} -e . "$file" >/dev/null 2>&1; then
          tmp="$(${pkgs.coreutils}/bin/mktemp "$file.unilib.XXXXXX")"
          ${lib.getExe pkgs.jq} --argjson s "$desired" ${lib.escapeShellArg jqMerge} "$file" > "$tmp"
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
