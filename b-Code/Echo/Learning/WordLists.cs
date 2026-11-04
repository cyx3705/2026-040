using System.IO.Compression;
using System.Text.RegularExpressions;
using System.Xml.Linq;

namespace Echo.Learning;

/// <summary>单词表里的一项。</summary>
internal sealed record WordItem(string Word, string Meaning, string Group);

/// <summary>
/// 从 Word（.docx）单词表里取词：.docx 是 zip，正文在 word/document.xml，按顺序读段落和表格。
/// 支持每段一个词（可跟释义「spacious 宽敞的」）与表格一行一个词；不是词的短段落当分组标题。
/// </summary>
internal static partial class WordLists
{
    private static readonly XNamespace W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";

    [GeneratedRegex(@"^([A-Za-z][A-Za-z'’\-]*(?: [A-Za-z][A-Za-z'’\-]*){0,3})(?=$|[\s/\[(（，,;；:：.]|[一-鿿])")]
    private static partial Regex EntryRx();

    [GeneratedRegex("[一-鿿]")] private static partial Regex CjkRx();
    [GeneratedRegex(@"^(/[^/]*/|\[[^\]]*\])")] private static partial Regex PhoneticRx();

    private static string Text(XElement element) => string.Concat(element.Descendants(W + "t").Select(t => t.Value)).Trim();

    private static string Squash(string text) => string.Join(' ', text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));

    /// <summary>一段文字是不是一个词条：是就返回 (词, 释义)；释义可能为空。</summary>
    internal static (string Word, string Meaning)? Entry(string text)
    {
        text = Squash(text);
        var m = EntryRx().Match(text);
        if (!m.Success)
            return null;
        var rest = PhoneticRx().Replace(text[m.Length..].Trim(), "", 1).Trim();
        if (rest.Length > 0 && !CjkRx().IsMatch(rest))
            return null;
        return (m.Groups[1].Value.Replace('’', '\''), rest);
    }

    /// <summary>按文档顺序返回词条，同一个词只留第一次。不是 .docx 时抛异常。</summary>
    public static List<WordItem> ParseDocx(byte[] data)
    {
        using var zip = new ZipArchive(new MemoryStream(data), ZipArchiveMode.Read);
        var part = zip.GetEntry("word/document.xml") ?? throw new InvalidDataException("不是 .docx");
        XDocument doc;
        using (var stream = part.Open())
            doc = XDocument.Load(stream);
        var body = doc.Root?.Element(W + "body");
        var group = "";
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var output = new List<WordItem>();

        void Add(string word, string meaning)
        {
            if (seen.Add(word))
                output.Add(new WordItem(word, meaning, group));
        }

        foreach (var el in body?.Elements() ?? [])
        {
            if (el.Name == W + "p")
            {
                var text = Text(el);
                if (text.Length == 0)
                    continue;
                if (Entry(text) is { } hit)
                    Add(hit.Word, hit.Meaning);
                else if (text.Length <= 40)
                    group = text;
            }
            else if (el.Name == W + "tbl")
            {
                foreach (var tr in el.Descendants(W + "tr"))
                {
                    var cells = tr.Descendants(W + "tc").Select(Text).ToList();
                    var hit = cells.Select(Entry).FirstOrDefault(h => h is not null);
                    if (hit is null)
                        continue;
                    var (word, meaning) = hit.Value;
                    if (meaning.Length == 0)
                        meaning = cells.FirstOrDefault(c => CjkRx().IsMatch(c)) ?? "";
                    Add(word, Squash(meaning));
                }
            }
        }

        return output;
    }
}
