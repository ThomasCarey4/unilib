# PyTorch's official CPU wheel (MKL + oneDNN, no CUDA). nixpkgs' source-built torch
# uses Eigen/OpenBLAS for BLAS, which made unilib's Qwen3 inference ~4x slower
# end to end (f32 matmul ~24x). Same version as nixpkgs' torch, so dependents agree.
{
  lib,
  stdenv,
  buildPythonPackage,
  fetchurl,
  python,
  autoPatchelfHook,
  cudaPackages,
  filelock,
  fsspec,
  jinja2,
  networkx,
  numpy,
  setuptools,
  sympy,
  typing-extensions,
}:

buildPythonPackage rec {
  pname = "torch";
  version = "2.14.1";
  format = "wheel";

  src = fetchurl {
    url = "https://download.pytorch.org/whl/cpu/torch-${version}%2Bcpu-cp314-cp314-manylinux_2_28_x86_64.whl";
    hash = "sha256-NRA+GAIUeTIH+V1+EuEA7Lo6zm9fs34fzvuwfglu0YA=";
  };

  nativeBuildInputs = [ autoPatchelfHook ];
  buildInputs = [ stdenv.cc.cc.lib ];

  dependencies = [
    filelock
    fsspec
    jinja2
    networkx
    numpy
    setuptools
    sympy
    typing-extensions
  ];

  postInstall = ''
    rm -rf $out/bin
  '';

  postFixup = ''
    addAutoPatchelfSearchPath "$out/${python.sitePackages}/torch/lib"
  '';

  pythonImportsCheck = [ "torch" ];

  # The attrs dependents read from nixpkgs' torch (e.g. torch.cudaSupport).
  passthru = {
    inherit cudaPackages;
    cudaSupport = false;
    rocmSupport = false;
    cudaCapabilities = [ ];
  };

  meta = {
    description = "PyTorch (official CPU wheel)";
    homepage = "https://pytorch.org";
    license = lib.licenses.bsd3;
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
    platforms = [ "x86_64-linux" ];
  };
}
