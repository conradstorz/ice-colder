cmake_minimum_required(VERSION 3.16.0)
# APPEND, never plain set(): PlatformIO passes -DEXTRA_COMPONENT_DIRS=<project src> and overwriting it
# would drop the project's own component.
list(APPEND EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../common/boardidf")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(vending)
