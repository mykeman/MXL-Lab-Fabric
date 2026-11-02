# MXL lab image: MXL SDK and tools (with MXL fabrics over libfabric), MXL and
# NDI GStreamer plugins, OpenCV, the YuNet face model, linuxptp and the RDMA
# userspace (rdma-core) for RoCEv2. One image for every lab function.
#
# UBUNTU is set by setup.sh to match the VM. Defaults to 24.04.
ARG UBUNTU=24.04
FROM ubuntu:${UBUNTU}

# Pinned upstream sources, so every build gets the same code. Update these
# deliberately and rebuild; scripts/pin-from-image.sh writes the versions an
# existing, working image was built from.
ARG RUST_TOOLCHAIN=1.98.1
ARG VCPKG_REF=07f4812200df3d3c931c0c8a6081d3b21fe2bf9f
ARG MXL_REF=d64a94837a61eccaabfa9b4d1313edd13b03acec
ARG GST_PLUGINS_RS_REF=8d285659a2d3db5760f90cfa6993391f6380b348
ARG OPENCV_VERSION=5.0.0.93
ARG YUNET_REF=47534e27c9851bb1128ccc0102f1145e27f23f98
ARG YUNET_SHA256=8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
# libfabric release MXL fabrics is developed against (its devcontainer uses it)
ARG LIBFABRIC_REF=v2.5.1

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git cmake ninja-build pkg-config \
      curl wget unzip zip tar ca-certificates clang lld bison flex \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
      gstreamer1.0-tools gstreamer1.0-plugins-base \
      gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
      gstreamer1.0-plugins-ugly gstreamer1.0-libav \
      python3 python3-pip python3-gi python3-numpy \
      gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
      libavahi-client3 libavahi-common3 \
      autoconf automake libtool \
      rdma-core libibverbs-dev librdmacm-dev ibverbs-providers ibverbs-utils \
      rdmacm-utils perftest \
    && rm -rf /var/lib/apt/lists/*

# Rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain "${RUST_TOOLCHAIN}"
ENV PATH=/root/.cargo/bin:$PATH

# vcpkg, required by the MXL CMake presets
# Full history: vcpkg resolves the MXL manifest's baseline from it.
RUN git clone https://github.com/microsoft/vcpkg.git /opt/vcpkg \
 && git -C /opt/vcpkg checkout --detach "${VCPKG_REF}" \
 && /opt/vcpkg/bootstrap-vcpkg.sh -disableMetrics
ENV VCPKG_ROOT=/opt/vcpkg

# NDI runtime, from the build context (not redistributable, keep this image internal)
COPY ndi/libndi.so.6 /usr/local/lib/libndi.so.6
RUN ln -sf /usr/local/lib/libndi.so.6 /usr/local/lib/libndi.so && ldconfig
ENV NDI_RUNTIME_DIR_V6=/usr/local/lib
ENV NDI_RUNTIME_DIR_V5=/usr/local/lib

# libfabric with the verbs provider (RoCEv2 through rdma-core) and tcp.
# Ubuntu's own libfabric is too old for MXL fabrics.
RUN git clone --depth 1 -b "${LIBFABRIC_REF}" https://github.com/ofiwg/libfabric.git /src/libfabric \
 && cd /src/libfabric && ./autogen.sh \
 && ./configure --prefix=/usr/local --enable-verbs=yes --enable-tcp=yes --enable-shm=yes \
      --disable-kdreg2 --disable-memhooks-monitor --disable-uffd-monitor \
 && make -j"$(nproc)" install && ldconfig

# MXL SDK and tools, with MXL fabrics (mxl-fabrics-demo, mxl-fabrics-info)
RUN git clone --filter=blob:none https://github.com/dmf-mxl/mxl.git /src/mxl \
 && git -C /src/mxl checkout --detach "${MXL_REF}"
# The MXL CMake preset looks for vcpkg under $HOME, which is /root in the build
RUN ln -sfn /opt/vcpkg /root/vcpkg
ENV CMAKE_POLICY_VERSION_MINIMUM=3.5
WORKDIR /src/mxl
ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig
RUN cmake --preset Linux-GCC-Release -DMXL_ENABLE_FABRICS_OFI=ON \
    || { tail -n 80 build/Linux-GCC-Release/vcpkg-manifest-install.log; exit 1; } \
 && cmake --build build/Linux-GCC-Release -j"$(nproc)" \
 && cp -a build/Linux-GCC-Release/lib/. /usr/local/lib/ \
 && find build -name 'libmxl-fabrics*.so*' -exec cp -a {} /usr/local/lib/ \; \
 && find build -maxdepth 5 -type f -executable -name 'mxl-*' \
      -exec cp {} /usr/local/bin/ \; \
 && ldconfig \
 && mxl-fabrics-demo --help >/dev/null && mxl-fabrics-info --help >/dev/null

# MXL GStreamer plugin
WORKDIR /src/mxl/rust
RUN cargo build --release

# NDI GStreamer plugin
RUN git clone --filter=blob:none \
      https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git /src/gst-plugins-rs \
 && git -C /src/gst-plugins-rs checkout --detach "${GST_PLUGINS_RS_REF}"
WORKDIR /src/gst-plugins-rs
RUN cargo build --release -p gst-plugin-ndi

# Consolidate both plugins
RUN mkdir -p /opt/gst-plugins \
 && cp /src/mxl/rust/target/release/libgstmxl.so /opt/gst-plugins/ \
 && cp /src/gst-plugins-rs/target/release/libgstndi.so /opt/gst-plugins/
ENV GST_PLUGIN_PATH=/opt/gst-plugins
ENV LD_LIBRARY_PATH=/usr/local/lib

# Fail the build here, not at runtime, if either plugin will not load
RUN gst-inspect-1.0 mxlsink >/dev/null && gst-inspect-1.0 ndisrc >/dev/null

# Autoframing deps
RUN pip3 install --break-system-packages --no-cache-dir \
      "opencv-python-headless==${OPENCV_VERSION}" \
 && mkdir -p /opt/models \
 && wget -qO /opt/models/face_detection_yunet_2023mar.onnx \
      "https://github.com/opencv/opencv_zoo/raw/${YUNET_REF}/models/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
 && echo "${YUNET_SHA256}  /opt/models/face_detection_yunet_2023mar.onnx" | sha256sum -c -

# gst-device-monitor-1.0 and friends, for debugging discovery by hand
RUN apt-get update && apt-get install -y --no-install-recommends \
      gstreamer1.0-plugins-base-apps \
    && rm -rf /var/lib/apt/lists/*

# PTP (linuxptp: ptp4l, phc2sys, pmc) and interface tools for the GUI, plus a
# check that the RTP and level elements used by aes67-out and the ST 2110
# functions are present.
RUN apt-get update && apt-get install -y --no-install-recommends \
      linuxptp ethtool iproute2 \
    && rm -rf /var/lib/apt/lists/* \
 && for e in rtpL24pay rtpL24depay rtpvrawpay rtpvrawdepay rtpjitterbuffer level; do \
      gst-inspect-1.0 "$e" >/dev/null || exit 1; done

# Baked-in copies. docker-compose.yml also bind-mounts these so edits
# on the host apply with a container restart.
COPY autoframe.py /opt/autoframe.py
COPY scripts/ /opt/scripts/
RUN chmod +x /opt/scripts/*

ENV MXL_DOMAIN=/dev/shm/mxl
WORKDIR /opt
