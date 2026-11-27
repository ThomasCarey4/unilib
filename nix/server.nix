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

  # Every library this server serves: the main one first (/mcp), then extraLibraries (/<name>/mcp).
  libraries = [
    {
      name = cfg.name;
      dir = cfg.libraryDir;
      webdavPort = cfg.webdav.port;
      indexTimer = null;
      unit = "webdav-unilib";
    }
  ]
  ++ lib.mapAttrsToList (name: l: {
    inherit name;
    inherit (l) dir indexTimer;
    webdavPort = l.webdavPort;
    unit = "webdav-unilib-${name}";
  }) cfg.extraLibraries;
  extra = builtins.tail libraries;
  davLibraries = builtins.filter (l: l.webdavPort != null) libraries;
  indexLibraries = builtins.filter (l: l.indexTimer != null) libraries;
  librariesEnv = lib.concatMapStringsSep "," (l: "${l.name}=${l.dir}") libraries;

  serverEnv = {
    UNILIB_ROOT = cfg.libraryDir;
    UNILIB_TOKEN_FILE = cfg.tokenFile;
    UNILIB_IDLE_EXIT_S = toString cfg.idleExitSeconds;
    UNILIB_MODEL_CACHE = cfg.modelCacheDir;
    HF_HOME = "${cfg.modelCacheDir}/hf-home";
  }
  // lib.optionalAttrs (extra != [ ]) { UNILIB_LIBRARIES = librariesEnv; }
  // cfg.environment;

  # The base config; at start `unilib webdav-config` adds read rules for each module
  # folder in modules.toml (webdav.readModules) and writes the config webdav uses.
  davBase = l: (pkgs.formats.json { }).generate "${l.unit}-base.json" {
    address = cfg.webdav.address;
    port = l.webdavPort;
    directory = l.dir;
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

    name = mkOption {
      type = types.strMatching "[a-z][a-z0-9-]*";
      default = "uni";
      description = "Name of the main library (served at /mcp); used by search(library=…) from other libraries.";
    };

    extraLibraries = mkOption {
      type = types.attrsOf (
        types.submodule {
          options = {
            dir = mkOption {
              type = types.str;
              example = "/home/alice/Documents/EngLibrary";
              description = "Library root (modules.toml, module folders, sources.toml, .index/).";
            };
            webdavPort = mkOption {
              type = types.nullOr types.port;
              default = null;
              description = "Port of a WebDAV share of this library (same rules and user as webdav); null: none.";
            };
            indexTimer = mkOption {
              type = types.nullOr types.str;
              default = null;
              example = "daily";
              description = ''
                systemd OnCalendar= for a periodic `unilib index` of this library, e.g. to pick up
                project lessons pulled into their repos; null: index only on demand.
              '';
            };
          };
        }
      );
      default = { };
      example = lib.literalExpression ''{ eng = { dir = "/home/alice/Documents/EngLibrary"; webdavPort = 8092; indexTimer = "daily"; }; }'';
      description = ''
        Further libraries served by the same process (sharing its models), each at
        http://host:port/<name>/mcp. Exported as UNILIB_LIBRARIES.
      '';
    };

    port = mkOption {
      type = types.port;
      default = 8095;
      description = "TCP port of the socket; the MCP endpoint is http://host:port/mcp (extra libraries: /<name>/mcp).";
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
      readModules = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Give the WebDAV user read access to each module folder listed in modules.toml (so
          clients can fetch originals). The rules are rebuilt when WebDAV starts, and WebDAV
          restarts whenever modules.toml changes. modules.toml, .index/ and any other files
          stay unreachable.
        '';
      };
      readableRegex = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "^/COMP[^/]*/";
        description = ''
          Extra paths (regex on the request path) the WebDAV user may read, beyond the module
          folders of readModules. null: none. Keep modules.toml, .index/ and notes outside it.
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

        assertions = [
          {
            assertion = !(builtins.elem cfg.name (builtins.attrNames cfg.extraLibraries))
              && lib.all (n: !(builtins.elem n [ "all" "lesson" "zotero" ])) (map (l: l.name) libraries);
            message = "services.unilib: library names must be unique and not all, lesson or zotero";
          }
        ];

        systemd.tmpfiles.rules =
          lib.concatMap (l: [
            "d ${l.dir}/_ingest 0755 ${cfg.user} ${cfg.group} -"
            "d ${l.dir}/_unsorted 0755 ${cfg.user} ${cfg.group} -"
          ]) libraries
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
          environment = serverEnv;
          serviceConfig = hardening // {
            User = cfg.user;
            Group = cfg.group;
            ExecStart = "${cfg.package}/bin/unilib-mcp";
            Restart = "no"; # an idle exit is normal; the socket starts it again
            ReadWritePaths = map (l: l.dir) libraries ++ lib.optional (cfg.modelCacheDir != defaultCache) cfg.modelCacheDir ++ cfg.readWritePaths;
            CacheDirectory = lib.mkIf (cfg.modelCacheDir == defaultCache) "unilib/models";
          };
        };
      }

      (lib.mkIf cfg.webdav.enable {
        # Keys are fixed here and only their values depend on the library list: a config-dependent
        # mkMerge at the top would make the module system evaluate cfg to find what it defines.
        systemd.services = lib.listToAttrs (
          lib.concatMap (l: [
            {
              name = l.unit;
              value = {
                description = "WebDAV share of the unilib library ${l.name} (ingest drop zone)";
                wantedBy = [ "multi-user.target" ];
                after = [ "network.target" ];
                environment.UNILIB_ROOT = l.dir;
                serviceConfig = hardening // {
                  User = cfg.user;
                  Group = cfg.group;
                  EnvironmentFile = cfg.webdav.environmentFile;
                  RuntimeDirectory = l.unit;
                  ExecStartPre =
                    if cfg.webdav.readModules then
                      "${cfg.package}/bin/unilib webdav-config ${davBase l} %t/${l.unit}/config.json"
                    else
                      "${pkgs.coreutils}/bin/cp ${davBase l} %t/${l.unit}/config.json";
                  ExecStart = "${lib.getExe pkgs.webdav} -c %t/${l.unit}/config.json";
                  Restart = "on-failure";
                  ReadWritePaths = [ "${l.dir}/_ingest" ];
                };
              };
            }
          ]
          ++ lib.optional cfg.webdav.readModules {
            name = "${l.unit}-modules";
            value = {
              description = "Restart ${l.unit} after modules.toml changes";
              serviceConfig = {
                Type = "oneshot";
                ExecStart = "${pkgs.systemd}/bin/systemctl try-restart ${l.unit}.service";
              };
            };
          }) davLibraries
        );

        # Module folders come from modules.toml: pick up added or renamed modules.
        systemd.paths = lib.mkIf cfg.webdav.readModules (
          lib.listToAttrs (
            map (l: {
              name = "${l.unit}-modules";
              value = {
                wantedBy = [ "paths.target" ];
                pathConfig.PathChanged = "${l.dir}/modules.toml";
              };
            }) davLibraries
          )
        );
      })

      {
        # Periodic re-index of a library (lessons live in project repos, outside the library).
        systemd.services = lib.listToAttrs (
          map (l: {
            name = "unilib-index-${l.name}";
            value = {
              description = "unilib index of the ${l.name} library";
              environment = serverEnv // { UNILIB_ROOT = l.dir; };
              serviceConfig = hardening // {
                Type = "oneshot";
                User = cfg.user;
                Group = cfg.group;
                ExecStart = "${cfg.package}/bin/unilib index";
                ReadWritePaths = [ l.dir ] ++ lib.optional (cfg.modelCacheDir != defaultCache) cfg.modelCacheDir ++ cfg.readWritePaths;
                CacheDirectory = lib.mkIf (cfg.modelCacheDir == defaultCache) "unilib/models";
                Nice = 10;
              };
            };
          }) indexLibraries
        );
        systemd.timers = lib.listToAttrs (
          map (l: {
            name = "unilib-index-${l.name}";
            value = {
              wantedBy = [ "timers.target" ];
              timerConfig = {
                OnCalendar = l.indexTimer;
                Persistent = true;
                RandomizedDelaySec = "30m";
              };
            };
          }) indexLibraries
        );
      }
    ]
  );
}
