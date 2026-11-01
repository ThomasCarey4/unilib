# NixOS module: the unilib MCP server, socket-activated, plus an optional WebDAV
# share of the library for the /ingest drop zone. Opens no firewall ports.
self:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.unilib;
  inherit (lib) mkOption types;
  defaultCache = "/var/cache/unilib/models";

  davConfig = (pkgs.formats.yaml { }).generate "webdav-unilib.yaml" {
    address = cfg.webdav.address;
    port = cfg.webdav.port;
    directory = cfg.libraryDir;
    permissions = "none";
    users = [
      {
        username = cfg.webdav.user;
        password = "{env}UNILIB_WEBDAV_PASSWORD";
        permissions = "none";
        # Applied last to first: the first match from the end wins.
        rules =
          lib.optional (cfg.webdav.readableRegex != null) {
            regex = cfg.webdav.readableRegex;
            permissions = "R";
          }
          ++ [
            {
              path = "/_ingest/";
              permissions = "CRUD";
            }
          ];
      }
    ];
  };

  hardening = {
    NoNewPrivileges = true;
    PrivateTmp = true;
    ProtectSystem = "strict";
    ProtectHome = cfg.protectHome;
  };
in
{
  options.services.unilib = {
    enable = lib.mkEnableOption "the unilib MCP server (streamable HTTP, socket-activated)";

    package = mkOption {
      type = types.package;
      default = pkgs.callPackage "${self}/nix/package.nix" { };
      defaultText = lib.literalExpression "unilib built from this pkgs";
      description = "The unilib package (with server dependencies).";
    };

    user = mkOption {
      type = types.str;
      default = "unilib";
      description = ''
        User the server runs as. The default creates a system user; set an existing user when the
        library, or the Zotero data and report bibs indexed as papers, live in that user's home.
      '';
    };

    group = mkOption {
      type = types.str;
      default = "unilib";
      description = "Group the server runs as.";
    };

    libraryDir = mkOption {
      type = types.str;
      example = "/home/alice/Documents/UniLibrary";
      description = "Library root (modules.toml, module folders, .index/). Exported as UNILIB_ROOT.";
    };

    port = mkOption {
      type = types.port;
      default = 8095;
      description = "TCP port of the socket; the MCP endpoint is http://host:port/mcp.";
    };

    idleExitSeconds = mkOption {
      type = types.ints.unsigned;
      default = 900;
      description = "Exit after this many seconds without requests (0: never). The socket restarts it.";
    };

    tokenFile = mkOption {
      type = types.str;
      description = "File holding the bearer token clients must send (Authorization: Bearer …).";
    };

    modelCacheDir = mkOption {
      type = types.str;
      default = defaultCache;
      description = "Hugging Face model cache (~2.4 GB once the Qwen3 models are downloaded).";
    };

    protectHome = mkOption {
      type = types.either types.bool (
        types.enum [
          "read-only"
          "tmpfs"
        ]
      );
      default = true;
      description = ''
        systemd ProtectHome= for the server. Use "read-only" when the library, the papers
        (Zotero storage, report bibs) or the model cache are under /home; writable paths
        under it are listed in readWritePaths (libraryDir and modelCacheDir are added).
      '';
    };

    readWritePaths = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Extra writable paths for the server (libraryDir and modelCacheDir are always writable).";
    };

    environment = mkOption {
      type = types.attrsOf types.str;
      default = { };
      example = {
        UNILIB_MAILTO = "me@example.com";
      };
      description = "Extra environment for the server.";
    };

    convert.package = mkOption {
      type = types.nullOr types.package;
      default = null;
      example = lib.literalExpression "pkgs.libreoffice";
      description = "LibreOffice for PPTX → PDF conversion in the ingest tools; null disables conversion.";
    };

    webdav = {
      enable = lib.mkEnableOption "a WebDAV share of the library: read/write _ingest/, optionally read-only module folders";
      address = mkOption {
        type = types.str;
        default = "0.0.0.0";
        description = "Bind address of the WebDAV server.";
      };
      port = mkOption {
        type = types.port;
        default = 8091;
        description = "WebDAV port.";
      };
      user = mkOption {
        type = types.str;
        default = "unilib";
        description = "WebDAV user name.";
      };
      environmentFile = mkOption {
        type = types.str;
        description = "File with UNILIB_WEBDAV_PASSWORD=… (systemd EnvironmentFile).";
      };
      readableRegex = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "^/COMP[^/]*/";
        description = ''
          Paths (regex on the request path) the WebDAV user may read, e.g. the module folders,
          so clients can fetch originals. null: only _ingest/ is reachable. Keep modules.toml,
          .index/, eval/ and any notes outside the match.
        '';
      };
    };
  };

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      {
        users.users = lib.mkIf (cfg.user == "unilib") {
          unilib = {
            isSystemUser = true;
            group = cfg.group;
          };
        };
        users.groups = lib.mkIf (cfg.group == "unilib") { unilib = { }; };

        systemd.tmpfiles.rules = [
          "d ${cfg.libraryDir}/_ingest 0755 ${cfg.user} ${cfg.group} -"
          "d ${cfg.libraryDir}/_unsorted 0755 ${cfg.user} ${cfg.group} -"
        ]
        ++ lib.optional (cfg.modelCacheDir != defaultCache) "d ${cfg.modelCacheDir} 0755 ${cfg.user} ${cfg.group} -";

        systemd.sockets.unilib = {
          description = "unilib MCP server socket";
          wantedBy = [ "sockets.target" ];
          listenStreams = [ (toString cfg.port) ];
        };

        systemd.services.unilib = {
          description = "unilib MCP server (socket-activated, exits when idle)";
          requires = [ "unilib.socket" ];
          after = [ "unilib.socket" ];
          path = lib.optional (cfg.convert.package != null) cfg.convert.package;
          environment = {
            UNILIB_ROOT = cfg.libraryDir;
            UNILIB_TOKEN_FILE = cfg.tokenFile;
            UNILIB_IDLE_EXIT_S = toString cfg.idleExitSeconds;
            UNILIB_MODEL_CACHE = cfg.modelCacheDir;
            HF_HOME = "${cfg.modelCacheDir}/hf-home";
          }
          // cfg.environment;
          serviceConfig = hardening // {
            User = cfg.user;
            Group = cfg.group;
            ExecStart = "${cfg.package}/bin/unilib-mcp";
            Restart = "no"; # an idle exit is normal; the socket starts it again
            ReadWritePaths = [ cfg.libraryDir ] ++ lib.optional (cfg.modelCacheDir != defaultCache) cfg.modelCacheDir ++ cfg.readWritePaths;
            CacheDirectory = lib.mkIf (cfg.modelCacheDir == defaultCache) "unilib/models";
          };
        };
      }

      (lib.mkIf cfg.webdav.enable {
        systemd.services.webdav-unilib = {
          description = "WebDAV share of the unilib library (ingest drop zone)";
          wantedBy = [ "multi-user.target" ];
          after = [ "network.target" ];
          serviceConfig = hardening // {
            User = cfg.user;
            Group = cfg.group;
            EnvironmentFile = cfg.webdav.environmentFile;
            ExecStart = "${lib.getExe pkgs.webdav} -c ${davConfig}";
            Restart = "on-failure";
            ReadWritePaths = [ "${cfg.libraryDir}/_ingest" ];
          };
        };
      })
    ]
  );
}
