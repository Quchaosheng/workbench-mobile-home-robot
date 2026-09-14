from glob import glob

from setuptools import find_packages, setup

package_name = "workbench_motion"

setup(
    name=package_name,
    version="0.2.0",
    packages=find_packages(exclude=["test", "test.*"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", glob("launch/*.launch.py")),
        ("share/" + package_name + "/config", glob("config/*.yaml") + glob("config/*.xacro") + glob("config/*.rviz")),
        ("share/" + package_name + "/config/moveit", glob("config/moveit/*")),
        # The workbench world xacro is owned by robot/description (not a ROS
        # package, no package.xml). We *vendor a copy into our share* at build
        # time so the composed URDF resolves via $(find workbench_motion) in BOTH
        # source and install space. See config/arm_on_workbench.urdf.xacro for why
        # $(find robot/description) is not an option.
        ("share/" + package_name + "/description", ["../../description/workbench.urdf.xacro"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Motion Owner",
    maintainer_email="motion-owner@workbench-1.invalid",
    description="Motion semantic-action adapter package: UR5e + Robotiq arm composition and reachability (phase 1).",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "scaffold_node = workbench_motion.scaffold_node:main",
            "reachability_check = workbench_motion.reachability_check:main",
            "phase2_probe = workbench_motion.phase2_probe:main",
            "motion_benchmark = workbench_motion.motion_benchmark:main",
        ],
    },
)
