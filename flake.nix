{
  description = "unilib: searchable lecture slides, notebooks and papers, served over MCP";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      overlays.default = final: _prev: {
        unilib = final.callPackage ./nix/package.nix { };
        unilib-client = final.callPackage ./nix/package.nix { server = false; };
      };

      packages = forAll (pkgs: rec {
        unilib = pkgs.callPackage ./nix/package.nix { };
        unilib-client = pkgs.callPackage ./nix/package.nix { server = false; };
        default = unilib;
      });

      # The server's pytest suite runs in its build (doCheck).
      checks = forAll (pkgs: {
        inherit (self.packages.${pkgs.stdenv.hostPlatform.system}) unilib unilib-client;
      });

      nixosModules.default = import ./nix/server.nix self;
      homeManagerModules.default = import ./nix/client.nix self;

      # Development keeps the uv workflow (uv.lock); `uv run pytest` inside.
      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            pkgs.uv
            pkgs.python3
          ];
          env.LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
            pkgs.stdenv.cc.cc.lib
            pkgs.zlib
          ];
        };
      });
    };
}
