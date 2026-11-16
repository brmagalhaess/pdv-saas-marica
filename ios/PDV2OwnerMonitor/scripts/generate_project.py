#!/usr/bin/env python3
"""Gera PDV2OwnerMonitor.xcodeproj (alvo PDV2Gestao + PDV2GestaoTests) a partir das pastas.

Uso (de ios/PDV2OwnerMonitor): python3 scripts/generate_project.py
Todo .swift em PDV2OwnerMonitor/ entra no app; Assets.xcassets e PrivacyInfo.xcprivacy
são recursos; os .plist são só referência (INFOPLIST_FILE). Em PDV2GestaoTests/, .swift
são testes e Fixtures/*.json são recursos do alvo de testes. IDs são estáveis (hash do caminho).
"""
import hashlib
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = "PDV2OwnerMonitor"
TEST_DIR = "PDV2GestaoTests"
CONFIG_DIR = "Config"
APP = "PDV2Gestao"
TESTS = "PDV2GestaoTests"
BUNDLE_ID = "br.com.crowntech.pdv2gestao"


def uid(*parts):
    return hashlib.sha1("/".join(parts).encode()).hexdigest()[:24].upper()


objects = {}


def add(oid, body):
    objects[oid] = body


def file_type(name):
    ext = os.path.splitext(name)[1]
    return {".swift": "sourcecode.swift", ".plist": "text.plist.xml", ".xcprivacy": "text.xml",
            ".xcassets": "folder.assetcatalog", ".json": "text.json", ".xcconfig": "text.xcconfig"}.get(ext, "text")


app_sources, app_resources, test_sources, test_resources = [], [], [], []
file_refs = {}


def walk_group(rel_dir, name=None, kind="app"):
    """Cria o PBXGroup de uma pasta (recursivo) e devolve o id."""
    gid = uid("group", rel_dir)
    children = []
    full = os.path.join(ROOT, rel_dir)
    entries = sorted(os.listdir(full), key=lambda e: (not os.path.isdir(os.path.join(full, e)) or e.endswith(".xcassets"), e))
    for entry in entries:
        if entry.startswith("."):
            continue
        rel = os.path.join(rel_dir, entry)
        path = os.path.join(ROOT, rel)
        if os.path.isdir(path) and not entry.endswith(".xcassets"):
            children.append((walk_group(rel, kind=kind), entry))
            continue
        fid = uid("file", rel)
        file_refs[rel] = fid
        add(fid, f'{{isa = PBXFileReference; lastKnownFileType = {file_type(entry)}; path = "{entry}"; sourceTree = "<group>"; }}')
        children.append((fid, entry))
        if kind == "app":
            if entry.endswith(".swift"):
                app_sources.append((rel, entry))
            elif entry.endswith((".xcassets", ".xcprivacy")):
                app_resources.append((rel, entry))
        elif kind == "tests":
            if entry.endswith(".swift"):
                test_sources.append((rel, entry))
            elif entry.endswith(".json"):
                test_resources.append((rel, entry))
    lines = ",\n".join(f"\t\t\t\t{c} /* {n} */" for c, n in children)
    add(gid, f'{{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n{lines},\n\t\t\t);\n\t\t\tpath = "{name or os.path.basename(rel_dir)}";\n\t\t\tsourceTree = "<group>";\n\t\t}}')
    return gid


app_group = walk_group(APP_DIR)
test_group = walk_group(TEST_DIR, kind="tests")
config_group = walk_group(CONFIG_DIR, kind="config")

app_product = uid("product", APP)
test_product = uid("product", TESTS)
add(app_product, f'{{isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = {APP}.app; sourceTree = BUILT_PRODUCTS_DIR; }}')
add(test_product, f'{{isa = PBXFileReference; explicitFileType = wrapper.cfbundle; includeInIndex = 0; path = {TESTS}.xctest; sourceTree = BUILT_PRODUCTS_DIR; }}')

products_group = uid("group", "Products")
add(products_group, f'{{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n\t\t\t\t{app_product} /* {APP}.app */,\n\t\t\t\t{test_product} /* {TESTS}.xctest */,\n\t\t\t);\n\t\t\tname = Products;\n\t\t\tsourceTree = "<group>";\n\t\t}}')
main_group = uid("group", "main")
add(main_group, f'{{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n\t\t\t\t{app_group} /* {APP_DIR} */,\n\t\t\t\t{test_group} /* {TEST_DIR} */,\n\t\t\t\t{config_group} /* {CONFIG_DIR} */,\n\t\t\t\t{products_group} /* Products */,\n\t\t\t);\n\t\t\tsourceTree = "<group>";\n\t\t}}')


def build_phase(kind, target, items, phase_name):
    pid = uid("phase", target, kind)
    files = []
    for rel, name in items:
        bid = uid("build", target, rel)
        add(bid, f'{{isa = PBXBuildFile; fileRef = {file_refs[rel]} /* {name} */; }}')
        files.append(f"\t\t\t\t{bid} /* {name} in {phase_name} */,")
    body = "\n".join(files)
    add(pid, f'{{\n\t\t\tisa = {kind};\n\t\t\tbuildActionMask = 2147483647;\n\t\t\tfiles = (\n{body}\n\t\t\t);\n\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t}}')
    return pid


app_src = build_phase("PBXSourcesBuildPhase", APP, app_sources, "Sources")
app_fw = build_phase("PBXFrameworksBuildPhase", APP, [], "Frameworks")
app_res = build_phase("PBXResourcesBuildPhase", APP, app_resources, "Resources")
test_src = build_phase("PBXSourcesBuildPhase", TESTS, test_sources, "Sources")
test_fw = build_phase("PBXFrameworksBuildPhase", TESTS, [], "Frameworks")
test_res = build_phase("PBXResourcesBuildPhase", TESTS, test_resources, "Resources")


def settings_block(d):
    out = []
    for k in sorted(d):
        v = d[k]
        if isinstance(v, list):
            items = "".join(f'\t\t\t\t\t"{i}",\n' for i in v)
            out.append(f"\t\t\t\t{k} = (\n{items}\t\t\t\t);")
        else:
            sv = str(v)
            if not sv or any(c in sv for c in ' $()/,-+"=') and not sv.startswith('"'):
                sv = '"' + sv.replace('"', '\\"') + '"'
            out.append(f"\t\t\t\t{k} = {sv};")
    return "\n".join(out)


def config(name, settings, base=None):
    cid = uid("config", *name)
    base_line = f"\t\t\tbaseConfigurationReference = {base};\n" if base else ""
    add(cid, f'{{\n\t\t\tisa = XCBuildConfiguration;\n{base_line}\t\t\tbuildSettings = {{\n{settings_block(settings)}\n\t\t\t}};\n\t\t\tname = {name[-1]};\n\t\t}}')
    return cid


common = {
    "ALWAYS_SEARCH_USER_PATHS": "NO",
    "CLANG_ENABLE_MODULES": "YES",
    "CLANG_ENABLE_OBJC_ARC": "YES",
    "ENABLE_STRICT_OBJC_MSGSEND": "YES",
    "ENABLE_USER_SCRIPT_SANDBOXING": "YES",
    "GCC_NO_COMMON_BLOCKS": "YES",
    "IPHONEOS_DEPLOYMENT_TARGET": "17.0",
    "LOCALIZATION_PREFERS_STRING_CATALOGS": "YES",
    "MARKETING_VERSION": "1.0.0",
    "CURRENT_PROJECT_VERSION": "1",
    "SDKROOT": "iphoneos",
    "SWIFT_VERSION": "5.0",
}
proj_debug = config(("project", "Debug"), {**common,
    "DEBUG_INFORMATION_FORMAT": "dwarf",
    "ENABLE_TESTABILITY": "YES",
    "GCC_OPTIMIZATION_LEVEL": "0",
    "ONLY_ACTIVE_ARCH": "YES",
    "SWIFT_ACTIVE_COMPILATION_CONDITIONS": "DEBUG",
    "SWIFT_OPTIMIZATION_LEVEL": "-Onone",
    "GCC_PREPROCESSOR_DEFINITIONS": ["DEBUG=1", "$(inherited)"],
})
proj_release = config(("project", "Release"), {**common,
    "DEBUG_INFORMATION_FORMAT": "dwarf-with-dsym",
    "ENABLE_NS_ASSERTIONS": "NO",
    "SWIFT_COMPILATION_MODE": "wholemodule",
    "SWIFT_OPTIMIZATION_LEVEL": "-O",
    "VALIDATE_PRODUCT": "YES",
})

app_common = {
    "ASSETCATALOG_COMPILER_APPICON_NAME": "AppIcon",
    "CODE_SIGN_STYLE": "Automatic",
    "DEVELOPMENT_TEAM": "",
    "ENABLE_PREVIEWS": "YES",
    "GENERATE_INFOPLIST_FILE": "NO",
    "LD_RUNPATH_SEARCH_PATHS": ["$(inherited)", "@executable_path/Frameworks"],
    "PRODUCT_BUNDLE_IDENTIFIER": BUNDLE_ID,
    "PRODUCT_NAME": APP,
    "SUPPORTS_MACCATALYST": "NO",
    "SUPPORTS_MAC_DESIGNED_FOR_IPHONE_IPAD": "NO",
    "SUPPORTED_PLATFORMS": "iphoneos iphonesimulator",
    "TARGETED_DEVICE_FAMILY": "1",
}
debug_xc = file_refs[os.path.join(CONFIG_DIR, "Debug.xcconfig")]
release_xc = file_refs[os.path.join(CONFIG_DIR, "Release.xcconfig")]
app_debug = config((APP, "Debug"), {**app_common, "INFOPLIST_FILE": f"{APP_DIR}/Resources/Info-Debug.plist"}, debug_xc)
app_release = config((APP, "Release"), {**app_common, "INFOPLIST_FILE": f"{APP_DIR}/Resources/Info.plist"}, release_xc)

test_common = {
    "BUNDLE_LOADER": "$(TEST_HOST)",
    "CODE_SIGN_STYLE": "Automatic",
    "DEVELOPMENT_TEAM": "",
    "GENERATE_INFOPLIST_FILE": "YES",
    "PRODUCT_BUNDLE_IDENTIFIER": f"{BUNDLE_ID}.tests",
    "PRODUCT_NAME": "$(TARGET_NAME)",
    "SUPPORTED_PLATFORMS": "iphoneos iphonesimulator",
    "TARGETED_DEVICE_FAMILY": "1",
    "TEST_HOST": f"$(BUILT_PRODUCTS_DIR)/{APP}.app/$(BUNDLE_EXECUTABLE_FOLDER_PATH)/{APP}",
}
test_debug = config((TESTS, "Debug"), test_common)
test_release = config((TESTS, "Release"), test_common)


def config_list(name, ids):
    lid = uid("configlist", name)
    body = "\n".join(f"\t\t\t\t{i}," for i in ids)
    add(lid, f'{{\n\t\t\tisa = XCConfigurationList;\n\t\t\tbuildConfigurations = (\n{body}\n\t\t\t);\n\t\t\tdefaultConfigurationIsVisible = 0;\n\t\t\tdefaultConfigurationName = Release;\n\t\t}}')
    return lid


proj_list = config_list("project", [proj_debug, proj_release])
app_list = config_list(APP, [app_debug, app_release])
test_list = config_list(TESTS, [test_debug, test_release])

project_id = uid("project")
app_target = uid("target", APP)
test_target = uid("target", TESTS)
proxy = uid("proxy", TESTS)
dependency = uid("dependency", TESTS)
add(proxy, f'{{\n\t\t\tisa = PBXContainerItemProxy;\n\t\t\tcontainerPortal = {project_id} /* Project object */;\n\t\t\tproxyType = 1;\n\t\t\tremoteGlobalIDString = {app_target};\n\t\t\tremoteInfo = {APP};\n\t\t}}')
add(dependency, f'{{\n\t\t\tisa = PBXTargetDependency;\n\t\t\ttarget = {app_target} /* {APP} */;\n\t\t\ttargetProxy = {proxy} /* PBXContainerItemProxy */;\n\t\t}}')
add(app_target, f'{{\n\t\t\tisa = PBXNativeTarget;\n\t\t\tbuildConfigurationList = {app_list};\n\t\t\tbuildPhases = (\n\t\t\t\t{app_src} /* Sources */,\n\t\t\t\t{app_fw} /* Frameworks */,\n\t\t\t\t{app_res} /* Resources */,\n\t\t\t);\n\t\t\tbuildRules = (\n\t\t\t);\n\t\t\tdependencies = (\n\t\t\t);\n\t\t\tname = {APP};\n\t\t\tproductName = {APP};\n\t\t\tproductReference = {app_product} /* {APP}.app */;\n\t\t\tproductType = "com.apple.product-type.application";\n\t\t}}')
add(test_target, f'{{\n\t\t\tisa = PBXNativeTarget;\n\t\t\tbuildConfigurationList = {test_list};\n\t\t\tbuildPhases = (\n\t\t\t\t{test_src} /* Sources */,\n\t\t\t\t{test_fw} /* Frameworks */,\n\t\t\t\t{test_res} /* Resources */,\n\t\t\t);\n\t\t\tbuildRules = (\n\t\t\t);\n\t\t\tdependencies = (\n\t\t\t\t{dependency} /* PBXTargetDependency */,\n\t\t\t);\n\t\t\tname = {TESTS};\n\t\t\tproductName = {TESTS};\n\t\t\tproductReference = {test_product} /* {TESTS}.xctest */;\n\t\t\tproductType = "com.apple.product-type.bundle.unit-test";\n\t\t}}')
add(project_id, f'''{{
\t\t\tisa = PBXProject;
\t\t\tattributes = {{
\t\t\t\tBuildIndependentTargetsInParallel = 1;
\t\t\t\tLastSwiftUpdateCheck = 2700;
\t\t\t\tLastUpgradeCheck = 2700;
\t\t\t\tTargetAttributes = {{
\t\t\t\t\t{app_target} = {{ CreatedOnToolsVersion = 27.0; }};
\t\t\t\t\t{test_target} = {{ CreatedOnToolsVersion = 27.0; TestTargetID = {app_target}; }};
\t\t\t\t}};
\t\t\t}};
\t\t\tbuildConfigurationList = {proj_list};
\t\t\tcompatibilityVersion = "Xcode 15.0";
\t\t\tdevelopmentRegion = "pt-BR";
\t\t\thasScannedForEncodings = 0;
\t\t\tknownRegions = (
\t\t\t\t"pt-BR",
\t\t\t\tBase,
\t\t\t);
\t\t\tmainGroup = {main_group};
\t\t\tproductRefGroup = {products_group} /* Products */;
\t\t\tprojectDirPath = "";
\t\t\tprojectRoot = "";
\t\t\ttargets = (
\t\t\t\t{app_target} /* {APP} */,
\t\t\t\t{test_target} /* {TESTS} */,
\t\t\t);
\t\t}}''')

body = "\n".join(f"\t\t{oid} = {objects[oid]};" for oid in sorted(objects))
pbx = f"""// !$*UTF8*$!
{{
\tarchiveVersion = 1;
\tclasses = {{
\t}};
\tobjectVersion = 60;
\tobjects = {{
{body}
\t}};
\trootObject = {project_id} /* Project object */;
}}
"""
proj_dir = os.path.join(ROOT, "PDV2OwnerMonitor.xcodeproj")
with open(os.path.join(proj_dir, "project.pbxproj"), "w", encoding="utf-8") as fh:
    fh.write(pbx)

scheme_dir = os.path.join(proj_dir, "xcshareddata", "xcschemes")
os.makedirs(scheme_dir, exist_ok=True)
ref = lambda target, name, product: (f'<BuildableReference BuildableIdentifier = "primary" BlueprintIdentifier = "{target}" '
                                     f'BuildableName = "{product}" BlueprintName = "{name}" '
                                     f'ReferencedContainer = "container:PDV2OwnerMonitor.xcodeproj"></BuildableReference>')
app_ref = ref(app_target, APP, f"{APP}.app")
test_ref = ref(test_target, TESTS, f"{TESTS}.xctest")
scheme = f'''<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion = "2700" version = "1.7">
   <BuildAction parallelizeBuildables = "YES" buildImplicitDependencies = "YES">
      <BuildActionEntries>
         <BuildActionEntry buildForTesting = "YES" buildForRunning = "YES" buildForProfiling = "YES" buildForArchiving = "YES" buildForAnalyzing = "YES">
            {app_ref}
         </BuildActionEntry>
      </BuildActionEntries>
   </BuildAction>
   <TestAction buildConfiguration = "Debug" selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB" shouldUseLaunchSchemeArgsEnv = "YES" language = "pt-BR" region = "BR">
      <Testables>
         <TestableReference skipped = "NO" parallelizable = "NO">
            {test_ref}
         </TestableReference>
      </Testables>
   </TestAction>
   <LaunchAction buildConfiguration = "Debug" selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB" launchStyle = "0" useCustomWorkingDirectory = "NO" ignoresPersistentStateOnLaunch = "NO" debugDocumentVersioning = "YES" debugServiceExtension = "internal" allowLocationSimulation = "YES" language = "pt-BR" region = "BR">
      <BuildableProductRunnable runnableDebuggingMode = "0">
         {app_ref}
      </BuildableProductRunnable>
   </LaunchAction>
   <ProfileAction buildConfiguration = "Release" shouldUseLaunchSchemeArgsEnv = "YES" savedToolIdentifier = "" useCustomWorkingDirectory = "NO" debugDocumentVersioning = "YES">
      <BuildableProductRunnable runnableDebuggingMode = "0">
         {app_ref}
      </BuildableProductRunnable>
   </ProfileAction>
   <AnalyzeAction buildConfiguration = "Debug"></AnalyzeAction>
   <ArchiveAction buildConfiguration = "Release" revealArchiveInOrganizer = "YES"></ArchiveAction>
</Scheme>
'''
with open(os.path.join(scheme_dir, f"{APP}.xcscheme"), "w", encoding="utf-8") as fh:
    fh.write(scheme)
print(f"{len(app_sources)} fontes do app, {len(app_resources)} recursos, {len(test_sources)} testes, {len(test_resources)} fixtures")
