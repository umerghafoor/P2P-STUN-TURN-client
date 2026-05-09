cmake_minimum_required(VERSION 3.15)
project(p2p_client CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Try to find libdatachannel installed system-wide first.
find_package(LibDataChannel QUIET)

if(NOT LibDataChannel_FOUND)
    # Fall back to fetching it. Adds ~5–10 minutes to a clean build.
    include(FetchContent)
    FetchContent_Declare(
        libdatachannel
        GIT_REPOSITORY https://github.com/paullouisageneau/libdatachannel.git
        GIT_TAG        v0.22.4
    )
    set(NO_MEDIA   ON  CACHE BOOL "" FORCE)  # we only need DataChannel
    set(NO_WEBSOCKET ON CACHE BOOL "" FORCE)
    set(NO_EXAMPLES ON CACHE BOOL "" FORCE)
    set(NO_TESTS    ON CACHE BOOL "" FORCE)
    FetchContent_MakeAvailable(libdatachannel)
endif()

add_executable(p2p_client client.cpp)
target_link_libraries(p2p_client PRIVATE LibDataChannel::LibDataChannel)
