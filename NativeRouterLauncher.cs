using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

internal static class NativeRouterLauncher
{
    private static int Main(string[] args)
    {
        try
        {
            string launcher = Process.GetCurrentProcess().MainModule.FileName;
            string root = Path.GetFullPath(Path.Combine(Path.GetDirectoryName(launcher), "..", ".."));
            string metadataPath = Path.Combine(root, "state", "native-runtime.json");
            var metadata = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(metadataPath));
            string configuredRoot = RequiredPath(metadata, "root", false);
            string python = RequiredPath(metadata, "python", true);
            string realCodex = RequiredPath(metadata, "real_codex", true);
            string bridge = Path.Combine(configuredRoot, "stdio_router.py");

            if (!String.Equals(root, configuredRoot, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Launcher location does not match native-runtime.json.");
            if (!File.Exists(bridge))
                throw new FileNotFoundException("Missing stdio router.", bridge);

            var childArgs = new List<string> { "-u", "-B", bridge };
            childArgs.AddRange(args);
            var start = new ProcessStartInfo(python, JoinArguments(childArgs));
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.RedirectStandardInput = true;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            NormalizeEnvironment(start);
            start.EnvironmentVariables["ROUTER_REAL_CODEX"] = realCodex;
            start.EnvironmentVariables["CODEX_CLI_PATH"] = realCodex;

            using (var child = Process.Start(start))
            {
                if (child == null)
                    throw new InvalidOperationException("Python bridge did not start.");

                Thread input = CopyThread(Console.OpenStandardInput(), child.StandardInput.BaseStream, true);
                Thread output = CopyThread(child.StandardOutput.BaseStream, Console.OpenStandardOutput(), false);
                Thread error = CopyThread(child.StandardError.BaseStream, Console.OpenStandardError(), false);
                child.WaitForExit();
                try { child.StandardInput.Close(); } catch { }
                output.Join();
                error.Join();
                GC.KeepAlive(input);
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("Adaptive Codex launcher: " + error.Message);
            return 2;
        }
    }

    private static string RequiredPath(Dictionary<string, object> metadata, string key, bool file)
    {
        object value;
        if (!metadata.TryGetValue(key, out value) || value == null)
            throw new InvalidDataException("Missing " + key + " in native-runtime.json.");
        string configured = Convert.ToString(value);
        if (!Path.IsPathRooted(configured))
            throw new InvalidDataException("Invalid " + key + " path in native-runtime.json.");
        string path = Path.GetFullPath(configured);
        if (file ? !File.Exists(path) : !Directory.Exists(path))
            throw new InvalidDataException("Invalid " + key + " path in native-runtime.json.");
        return path;
    }

    private static void NormalizeEnvironment(ProcessStartInfo start)
    {
        var environment = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (DictionaryEntry item in start.EnvironmentVariables)
            environment[Convert.ToString(item.Key)] = Convert.ToString(item.Value);
        start.EnvironmentVariables.Clear();
        foreach (var item in environment)
            start.EnvironmentVariables[item.Key] = item.Value;
    }

    private static Thread CopyThread(Stream source, Stream destination, bool closeDestination)
    {
        var thread = new Thread(delegate()
        {
            try
            {
                byte[] buffer = new byte[65536];
                int count;
                while ((count = source.Read(buffer, 0, buffer.Length)) > 0)
                {
                    destination.Write(buffer, 0, count);
                    destination.Flush();
                }
            }
            catch (IOException) { }
            catch (ObjectDisposedException) { }
            finally
            {
                if (closeDestination)
                    try { destination.Close(); } catch { }
            }
        });
        thread.IsBackground = true;
        thread.Start();
        return thread;
    }

    private static string JoinArguments(IEnumerable<string> args)
    {
        var result = new StringBuilder();
        foreach (string arg in args)
        {
            if (result.Length > 0) result.Append(' ');
            result.Append(QuoteArgument(arg));
        }
        return result.ToString();
    }

    private static string QuoteArgument(string value)
    {
        if (value.Length > 0 && value.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '"' }) < 0)
            return value;
        var quoted = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\')
            {
                slashes++;
                continue;
            }
            if (character == '"')
                quoted.Append('\\', slashes * 2 + 1);
            else
                quoted.Append('\\', slashes);
            quoted.Append(character);
            slashes = 0;
        }
        quoted.Append('\\', slashes * 2);
        quoted.Append('"');
        return quoted.ToString();
    }
}
