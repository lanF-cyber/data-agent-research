#requires -Version 7.3
[CmdletBinding()]
param(
    [string]$Lab = (Join-Path $env:USERPROFILE 'rsi-lab'),
    [string]$Materials = (Join-Path $Lab 'materials')
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$Lab = [IO.Path]::GetFullPath($Lab)
$materials = (Get-Item -LiteralPath $Materials).FullName
$tools = Join-Path $Lab 'tools'
$front = Join-Path $Lab 'rsi-data-platform'
$visual = Join-Path $Lab 'rsi-data-visual'
$back = Join-Path $Lab 'rsi-data-spring-boot'
$settings = (Get-Item -LiteralPath "$materials\settings.xml").FullName
$null = Get-Item -LiteralPath "$materials\0.1.2.zip"
$null = New-Item -ItemType Directory -Force -Path "$tools\jdk8","$tools\maven","$Lab\data"

if (-not $env:NVM_HOME) { $env:NVM_HOME = [Environment]::GetEnvironmentVariable('NVM_HOME', 'User') }
if (-not $env:NVM_SYMLINK) { $env:NVM_SYMLINK = [Environment]::GetEnvironmentVariable('NVM_SYMLINK', 'User') }
$env:Path = "$env:NVM_HOME;$env:NVM_SYMLINK;$env:Path"
python -c "import sys; assert sys.version_info >= (3, 10), 'Python 3.10+ required'"


if (!(Test-Path "$env:NVM_HOME\v18.20.8\node.exe")) { nvm install 18.20.8 64 }
if (!(Test-Path "$env:NVM_HOME\v24.14.1\node.exe")) { nvm install 24.14.1 64 }
nvm use 24.14.1
$env:Path = "$env:NVM_SYMLINK;$env:NVM_HOME;$env:Path"
$node24 = Join-Path $env:NVM_SYMLINK 'node.exe'
$node18 = Join-Path $env:NVM_HOME 'v18.20.8\node.exe'
nvm current
& $node24 -v
& $node18 -v

function Get-CheckedFile($url, $file, $sha256) {
    if ((Test-Path -LiteralPath $file) -and (Get-FileHash -LiteralPath $file).Hash -eq $sha256) {
        return
    }
    $download = "$file.part"
    curl.exe -fLsS --retry 2 --connect-timeout 15 --max-time 300 -o $download $url
    if ((Get-FileHash -LiteralPath $download).Hash -ne $sha256) {
        throw "SHA-256 mismatch: $download"
    }
    Move-Item -LiteralPath $download -Destination $file -Force
}
Get-CheckedFile 'https://corretto.aws/downloads/resources/8.514.01.1/amazon-corretto-8.514.01.1-windows-x64-jdk.zip' "$tools\jdk.zip" '73AAA322A40C9A9786075618E16384E3CADD83EF9C264AE48CF1AFBD4B5F549C'
Get-CheckedFile 'https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.16/apache-maven-3.9.16-bin.zip' "$tools\maven.zip" '5AF3B743DD8B876B5C45DA33B676251E5F1687712644ABB4EE519CA56E1D89CE'
Get-CheckedFile 'https://repo.maven.apache.org/maven2/com/h2database/h2/1.4.199/h2-1.4.199.jar' "$tools\h2-1.4.199.jar" '3125A16743BC6B4CFBB61ABBA783203F1FB68230AA0FDC97898F796F99A5D42E'
tar.exe -xf "$tools\jdk.zip" --strip-components=1 -C "$tools\jdk8"
tar.exe -xf "$tools\maven.zip" --strip-components=1 -C "$tools\maven"
$env:JAVA_HOME = "$tools\jdk8"
$env:Path = "$env:JAVA_HOME\bin;$env:Path"
$env:MAVEN_OPTS = '-Dfile.encoding=UTF-8'
$mvn = "$tools\maven\bin\mvn.cmd"
java -version
& $mvn --version

function Get-PinnedSource($dir, $url, $branch, $revision) {
    if (!(Test-Path -LiteralPath $dir)) {
        git clone --branch $branch --single-branch $url $dir
        git -C $dir checkout --detach $revision
    }
    if ((git -C $dir rev-parse HEAD) -ne $revision) { throw "Unexpected source revision in $dir" }
}
Get-PinnedSource $visual 'https://github.com/ymxiong/rsi-data-visual.git' 'dev_main' 'b80f121c3752f5cb2b566983f0ba664c57c9d13c'
Get-PinnedSource $front 'https://github.com/ymxiong/rsi-data-platform.git' 'dev_main' '29ecb6c0b3abe32a5df2a649b22085377c86fce7'
Get-PinnedSource $back 'https://github.com/ymxiong/rsi-data-spring-boot.git' 'master' '58051d9b7e679e9730e0a431fc5229cc01f972dd'


& "$env:NVM_SYMLINK\npm.cmd" install --prefix "$tools\js" --no-save --no-audit --no-fund pnpm@11.25.0 esbuild@0.17.19
Push-Location $visual
try {
    & $node18 "$tools\js\node_modules\esbuild\bin\esbuild" src/index.ts --bundle --platform=browser --format=cjs --packages=external --loader:.svg=dataurl --outfile=dist/index.js
} finally { Pop-Location }
& $node24 "$tools\js\node_modules\pnpm\bin\pnpm.cjs" --dir $front install --frozen-lockfile --store-dir "$Lab\pnpm-store"
$built = (Get-FileHash "$visual\dist\index.js").Hash
$installed = (Get-FileHash "$front\node_modules\@horsecoder\visual-core\dist\index.js").Hash
if ($built -ne $installed) { throw 'Visual build and installed copy differ' }

$route = Join-Path $front 'app\harness'
$null = New-Item -ItemType Directory -Force -Path $route
$files = @('platform\page.tsx', 'platform\layout.tsx', 'platform\harness.css', 'harness\workflow.json')
foreach ($name in $files) {
    $source = Join-Path $PSScriptRoot $name
    $target = Join-Path $route ([IO.Path]::GetFileName($name))
    if (Test-Path -LiteralPath $target) {
        if ((Get-FileHash $source).Hash -ne (Get-FileHash $target).Hash) { throw "Existing route file differs: $target" }
    } else {
        Copy-Item -LiteralPath $source -Destination $target
    }
}

$change = Join-Path $PSScriptRoot 'flow-result.patch'
$sourceFile = 'core-flow/src/main/java/com/horsecoder/common/whuTalent/core/flow/Run/code/FlowCodeRun.java'
$expected = 'e5b076196db6875a3ca9d1989da631ef1581010e'
if ((git -C $back hash-object $sourceFile) -ne $expected) {
    git -C $back apply --check --unidiff-zero $change
    git -C $back apply --unidiff-zero $change
}
if ((git -C $back hash-object $sourceFile) -ne $expected) { throw 'Flow node checksum mismatch' }

$database = Join-Path $Lab 'data\flight_2.sqlite'
$sample = Join-Path $materials 'flight_2.sqlite'
if (Test-Path -LiteralPath $sample) {
    Copy-Item -LiteralPath $sample -Destination $database -Force
} else {
    $archive = [IO.Compression.ZipFile]::OpenRead((Get-Item "$materials\cosql_dataset.zip").FullName)
    try {
        $entry = $archive.GetEntry('cosql_dataset/database/flight_2/flight_2.sqlite')
        if (!$entry) { throw 'flight_2.sqlite is missing from the CoSQL archive' }
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $database, $true)
    } finally { $archive.Dispose() }
}
if ((Get-FileHash $database).Hash -ne 'DB777BA6488AE2B515CB138D71FF23C4533E5FD0355854648A1102FAFF3FB7AF') { throw 'Unexpected CoSQL database' }


$courseDeps = Join-Path $tools 'course-deps'
$null = New-Item -ItemType Directory -Path $courseDeps -Force
tar.exe -xf "$materials\0.1.2.zip" -C $courseDeps '0.1.2/scheduler-core-0.1.2.jar' '0.1.2/scheduler-core-0.1.2.pom'
$scheduler = "$courseDeps\0.1.2\scheduler-core-0.1.2"
if ((Get-FileHash "$scheduler.jar").Hash -ne '042BAEF052033F3445D47F5B0CC69035FBE4330954FD5B48A1C75D7E9FC51AED') { throw 'Unexpected scheduler JAR' }
$mavenArgs = @('-B', '-ntp', '-s', $settings, "-Dmaven.repo.local=$Lab\m2")
& $mvn @mavenArgs 'org.apache.maven.plugins:maven-install-plugin:3.1.4:install-file' "-Dfile=$scheduler.jar" "-DpomFile=$scheduler.pom"
& $mvn @mavenArgs -f "$back\pom.xml" -pl core-starter -am '-Dmaven.test.skip=true' clean install
