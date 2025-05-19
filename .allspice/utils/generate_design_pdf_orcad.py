import os
import time
import json
import uuid
import pymupdf
import zipfile
import cairosvg
import subprocess
from allspice import AllSpice
from argparse import ArgumentParser
from xml.etree import ElementTree as ET

###############################################################################
GET_LAST_COMMIT_ON_TARGET_BRANCH_ENDPOINT = """/repos/{owner}/{repo}/commits?sha={ref}&limit=1"""


###############################################################################
def get_dsn_files_from_previous_commit(commit_files):
    design_doc_paths = []
    for file in commit_files:
        if file["filename"].lower().endswith(".dsn"):
            if file["status"] != "removed":
                design_doc_paths.append(file["filename"])
    return design_doc_paths


###############################################################################
def push_changes_on_target_branch(ref, sha):
    # Pull first before pushing
    git_add_output = subprocess.check_output(
        ["git pull"], shell=True, encoding="utf-8"
    )
    # Add files for commit
    git_add_output = subprocess.check_output(
        ["git add ."], shell=True, encoding="utf-8"
    )
    try:
        # Add a commit message
        git_commit_output = subprocess.check_output(
            ['git commit -m "PDF generated from design update in ' + sha[0:10] + '"'],
            shell=True,
            encoding="utf-8",
        )
        # Push to remote
        print("- Pushing extracted files to the " + "'" + ref + "' branch")
        git_push_process = subprocess.run(
            "git push", capture_output=True, text=True, shell=True
        )
    except subprocess.CalledProcessError as e:
        print(e.returncode)
        print(e.output)


###############################################################################
def set_git_config(name, email):
    git_config_name_output = subprocess.check_output(
        ["git config --global user.name " + '"' + name + '"'],
        shell=True,
        encoding="utf-8",
    )
    git_config_email_output = subprocess.check_output(
        ["git config --global user.email " + '"' + email + '"'],
        shell=True,
        encoding="utf-8",
    )


###############################################################################
def get_previous_commit_on_target_branch(client, repository, branch, auth_token):
    time.sleep(0.1)
    commits_json = client.requests_get(
        GET_LAST_COMMIT_ON_TARGET_BRANCH_ENDPOINT.format(
            owner=repository.owner.username, repo=repository.name, ref=branch, token=auth_token
        )
    )
    # Set author as the same person who made the previous commit
    name = commits_json[0]["commit"]["committer"]["name"]
    email = commits_json[0]["commit"]["committer"]["email"]
    sha = commits_json[0]["sha"]
    files = commits_json[0]["files"]
    return name, email, sha, files


###############################################################################
def split_multipage_svg(svg_text: str) -> list[str]:
    """
    Split a multi-page SVG into individual SVG files, one for each page.
    Uses ElementTree for proper XML parsing.

    Args:
        svg_text (str): The content of the multi-page SVG file.
    Returns:
        list: List of the SVG contents for each page.
    """

    ET.register_namespace("", "http://www.w3.org/2000/svg")
    parser = ET.XMLParser(encoding="utf-8")

    root = ET.fromstring(svg_text, parser=parser)

    children = list(root)

    # Each pair of <style> and <g> is one page.
    page_pairs = []

    for i in range(len(children) - 1):
        current = children[i]
        next_elem = children[i + 1]

        if current.tag.endswith("}style") and next_elem.tag.endswith("}g"):
            page_pairs.append((current, next_elem))

    output_files = []

    for i, (style_elem, g_elem) in enumerate(page_pairs):
        new_svg = ET.Element("svg")

        for attr, value in root.attrib.items():
            new_svg.set(attr, value)

        original_id = root.get("id", "")
        new_svg.set("id", f"{original_id}" if original_id else f"page-{i + 1}")

        width = g_elem.get("data-width")
        height = g_elem.get("data-height")
        view_box: str = g_elem.get("data-view-box")

        if width:
            new_svg.set("width", width)
        if height:
            new_svg.set("height", height)

        new_svg.set("viewBox", view_box)

        new_svg.append(style_elem)
        del g_elem.attrib["transform"]
        new_svg.append(g_elem)

        svg_str = ET.tostring(new_svg, encoding="unicode")

        output_files.append(svg_str)

    return output_files


###############################################################################
def generate_orcad_pdfs(design_doc_paths, repository, commit_to_branch, title_block_field, sha, name, branch):
    # Initialize an XML parser
    parser = ET.XMLParser(encoding="utf-8")
    # Declare commit link
    commit_link = repository.url + "/commit/" + sha
    # For all design files, generate PDFs
    for design_doc in design_doc_paths:
        print("- Processing " + design_doc)
        # Get SVG of schematic
        retries = 1
        fetched = False
        while not fetched and retries < 6:
            try:
                time.sleep(0.1)
                svg = repository.get_generated_svg(design_doc, ref=branch)
                fetched = True
            except Exception:
                print("- Generateing svg in progress. Trying again in ", end="")
                for t in range(0, 5, -1):
                    print(str(t) + "...")
                print(" " + str(5 - retries) + " retries left", end="", flush=True)
                time.sleep(1)
                retries += 1
        # If a title block field is specified, try to populate sha link in title block
        if title_block_field is not None:
            # Initialize an ElementTree for the svg
            tree = ET.fromstring(svg, parser=parser)
            # Find all title block fields with the matching field name
            title_block_field_matches = tree.findall('.//{http://www.w3.org/2000/svg}text[@data-id="' + title_block_field + '"]')
            for field_match in title_block_field_matches:
                for field_value in field_match.iter('{http://www.w3.org/2000/svg}tspan'):
                    field_value.text += "    |    AllSpice Commit: "
                    linktag = ET.SubElement(field_value, '{http://www.w3.org/2000/svg}a')
                    linktag.set("href", commit_link)
                    linktag.set("class", "color")
                    linktag.set("style", "fill:blue")
                    linktag.text = sha[0:10]
                    linktag.tail = " by " + name
            xmlstr = ET.tostring(tree, encoding='utf8')
        else:
            xmlstr = svg
        # Split the schematic pages into individual svgs
        schematic_pages_svgs = split_multipage_svg(xmlstr)
        # Create a directory to store the PDFs
        dir_name = str(uuid.uuid4())
        os.mkdir("/tmp/" + dir_name)
        # Create a PDF writer to write merged PDFs
        doc = pymupdf.open()
        pdf_filename = os.path.splitext(os.path.basename(design_doc))[0] + ".pdf"
        # Loop through pages and generate/merge PDFs for each sheet
        for page_num, page in enumerate(schematic_pages_svgs):
            # Create PDF from each SVG
            pdf_filepath = "/tmp/" + dir_name + "/" + str(page_num) + "_" + pdf_filename
            cairosvg.svg2pdf(bytestring=page, write_to=pdf_filepath)
            print("- Generating PDF for " + pdf_filepath)
            # Append each PDF sheet to the writer
            doc.insert_pdf(pymupdf.open(pdf_filepath))
        # Add sha links to the PDF
        for pagenum, page in enumerate(doc): # iterate the document pages
            matches = page.search_for(sha[0:10], quads=True)
            for match in matches:
                linkdict = {
                    'kind' : 2,
                    'from' : match.rect,
                    'page' : pagenum,
                    'to' : None,
                    'file' : None,
                    'uri' : repository.html_url + "/commit/" + sha,
                    'xref' : pagenum
                }
            page.insert_link(linkdict)
        # Write merged PDF file
        print("- Saving merged PDF to artifacts folder " + "/pdfs/" + pdf_filename)
        doc.save("/pdfs/" + pdf_filename)
        # Write file back to branch if specified
        if commit_to_branch:
            repo_pdfpath = os.path.splitext(design_doc)[0] + ".pdf"
            print("- Saving PDF to repo path " + repo_pdfpath)
            doc.save(repo_pdfpath)


###############################################################################
if __name__ == "__main__":
    # Initialize argument parser
    parser = ArgumentParser()
    parser.add_argument(
        "repository", help="Repository object for the target repo"
    )
    parser.add_argument(
        "ref", help="Target branch"
    )
    parser.add_argument(
        "sha", help="Commit hash that triggered this action"
    )
    parser.add_argument(
        "--allspice_hub_url",
        help="The URL of your AllSpice Hub instance. Defaults to https://hub.allspice.io.",
    )
    parser.add_argument(
        "--title_block_field", help="Title block field name for the commit hash to be populated",
    )
    parser.add_argument(
        "--commit_to_branch", action='store_true', help="Commit PDF back to branch? ",
    )
    args = parser.parse_args()
    # Get auth token and hub url
    auth_token = os.environ.get("ALLSPICE_AUTH_TOKEN")
    if auth_token is None:
        print("Please set the environment variable ALLSPICE_AUTH_TOKEN")
        exit(1)
    if args.allspice_hub_url is None:
        allspice = AllSpice(token_text=auth_token)
    else:
        allspice = AllSpice(
            token_text=auth_token,
            allspice_hub_url=args.allspice_hub_url,
        )
    # Get the repository
    repo_owner, repo_name = args.repository.split("/")
    repository = allspice.get_repository(repo_owner, repo_name)
    # Get the previous commit
    name, email, sha, files = get_previous_commit_on_target_branch(allspice, repository, args.sha, auth_token)
    # Get filepaths to any new or modified design files in the previous commmit
    design_doc_paths = get_dsn_files_from_previous_commit(files)
    # Process extracting
    if design_doc_paths:
        # Generate a PDF for every schematic in the list
        try:
            os.makedirs("/pdfs")
        except FileExistsError:
            pass
        # Generate PDF
        generate_orcad_pdfs(design_doc_paths, repository, args.commit_to_branch, args.title_block_field, sha, name, args.ref)
        # Commit back to the repo if specified
        if args.commit_to_branch:
            # Set git config user and email
            set_git_config(name, email)
            # Push changes to the target branch
            push_changes_on_target_branch(args.ref, sha)
            # Zip all the generated PDFs
        with zipfile.ZipFile("pdfs.zip", "w", zipfile.ZIP_DEFLATED
        ) as zipper:
            for root, dirs, files in os.walk("/pdfs"):
                for file in files:
                    zipper.write(os.path.join(root, file))
    else:
        print("- No design files were added or modified in the previous commit.")
