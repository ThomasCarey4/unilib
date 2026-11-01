# unilib from nixpkgs' python3Packages. `server = false` builds the light client
# (unilib-client, unilib-local-mcp): stdlib + mcp, no torch or pymupdf.
{
  lib,
  stdenv,
  python3,
  server ? true,
  # Use PyTorch's CPU wheel (MKL) instead of nixpkgs' source build (see torch-cpu.nix).
  torchWheel ? stdenv.hostPlatform.system == "x86_64-linux" && python3.pythonVersion == "3.14",
}:

let
  python =
    if server && torchWheel then
      python3.override {
        self = python;
        packageOverrides = pyfinal: pyprev: {
          torch = pyfinal.callPackage ./torch-cpu.nix { };
          # Their test inputs (torchvision, torchaudio, …) are C++ builds against torch;
          # skip the tests rather than rebuild those against the wheel.
          sentence-transformers = pyprev.sentence-transformers.overridePythonAttrs { doCheck = false; };
          transformers = pyprev.transformers.overridePythonAttrs { doCheck = false; };
        };
      }
    else
      python3;
  python3Packages = python.pkgs;

  serverDeps = with python3Packages; [
    pymupdf
    numpy
    sentence-transformers
    transformers
    torch
  ];
in
python3Packages.buildPythonApplication {
  pname = if server then "unilib" else "unilib-client";
  version = (lib.importTOML ../pyproject.toml).project.version;
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../LICENSE
      ../src
      ../tests
      ../skills
    ];
  };

  build-system = [ python3Packages.hatchling ];
  dependencies = [ python3Packages.mcp ] ++ lib.optionals server serverDeps;

  # The client imports only the stdlib and mcp; the server's checks cover the code.
  doCheck = server;
  nativeCheckInputs = [ python3Packages.pytestCheckHook ];
  # Real models (slow) and the real library (eval) are not available in the sandbox.
  pytestFlags = [
    "-m"
    "not slow and not eval"
  ];
  preCheck = ''
    export HOME=$TMPDIR
  '';

  postInstall = ''
    mkdir -p $out/share/unilib/skills
    cp -r skills/ingest $out/share/unilib/skills/
  ''
  + lib.optionalString (!server) ''
    rm $out/bin/unilib $out/bin/unilib-mcp
  '';

  pythonImportsCheck = [ "unilib.client_cli" ] ++ lib.optionals server [ "unilib.server" ];

  meta = {
    description = "Searchable library of lecture slides, notebooks and papers, served over MCP";
    homepage = "https://github.com/ThomasCarey4/unilib";
    license = lib.licenses.mit;
    mainProgram = if server then "unilib-mcp" else "unilib-client";
  };
}
